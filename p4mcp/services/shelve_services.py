"""
P4 shelve services layer

Read service for tools:
- list_shelves : List shelved changelists
- get_shelve_diff : Get shelve diff
- get_shelve_files : Get shelved files

Write service for tools:
- shelve_files : Shelve files
- unshelve_files : Unshelve files
- delete_shelve : Delete shelve
- update_shelve : Update shelve
- unshelve_to_changelist : Unshelve to changelist

"""

import logging
from typing import List, Dict, Any, Optional, Mapping
from P4 import P4Exception

from ..core.connection import P4ConnectionManager
from .review_diff import build_hunks, change_kind, looks_binary, parse_unified_diff

logger = logging.getLogger(__name__)

_MAX_STRUCTURED_DIFF_OUTPUT_BYTES = 64 * 1024 * 1024


def _expand_describe_files(records: Any) -> list[dict[str, Any]]:
    """Expand P4 tagged describe arrays into one metadata object per file."""
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, list) or not all(isinstance(item, Mapping) for item in records):
        raise ValueError("p4 describe -S returned malformed metadata")
    if not records:
        raise ValueError("p4 describe -S returned no changelist metadata")
    expanded: list[dict[str, Any]] = []
    for record in records:
        # A normal tagged ``describe`` call returns one record whose file
        # attributes are parallel arrays.  Accept scalar values as well for
        # lightweight P4 proxies and test doubles.
        paths = record.get("depotFile", [])
        if isinstance(paths, str):
            paths = [paths]
        elif isinstance(paths, tuple):
            paths = list(paths)
        if not isinstance(paths, list):
            raise ValueError("p4 describe -S returned a malformed depotFile list")
        if not paths:
            # A pending changelist record without depotFile entries is a
            # changelist with no shelf, not a file whose path is unknown.
            # The caller must be able to distinguish this from a malformed
            # per-file record and fail closed.
            continue

        count = len(paths)
        for key in ("action", "type", "rev", "fileSize", "digest"):
            value = record.get(key)
            if isinstance(value, (list, tuple)) and len(value) not in (0, count):
                raise ValueError(
                    f"p4 describe -S field {key!r} has {len(value)} values for {count} files")

        def value_at(key: str, index: int) -> Any:
            value = record.get(key)
            if isinstance(value, (list, tuple)):
                return value[index] if index < len(value) else None
            # Some lightweight P4 proxies return a scalar for a field that is
            # uniform across all depotFile entries. Treat it as a broadcast
            # value instead of making every subsequent file malformed.
            return value

        for index, path in enumerate(paths):
            if not isinstance(path, str) or not path:
                raise ValueError("p4 describe -S returned a malformed depotFile")
            item: dict[str, Any] = {"depotFile": path}
            for key in ("action", "type", "rev", "fileSize", "digest"):
                value = value_at(key, index)
                if value is not None:
                    item[key] = value
            expanded.append(item)
    return expanded


def _shelf_presence(action: Any) -> tuple[bool, bool]:
    if action is None or not str(action).strip():
        return False, False
    kind = change_kind(action)
    if kind == "add":
        return False, True
    if kind == "delete":
        return True, False
    if kind == "unknown":
        return False, False
    return True, True


def _apply_shelf_revision_refs(
        item: dict[str, Any], metadata: Mapping[str, Any], changelist_id: str) -> None:
    """Attach authoritative before/after refs to one structured shelf file.

    The textual ``describe -du`` stream often contains only a display
    revision (and, for add/delete, no hunk at all).  The tagged ``describe
    -S`` inventory is the source of truth for the action and shelved revision,
    so derive refs from it instead of trusting parser header fields.
    """
    action = item.get("action") or metadata.get("action")
    if action is not None and str(action).strip():
        item["action"] = action
    if metadata.get("type") is not None:
        item.setdefault("type", metadata.get("type"))
    kind = change_kind(action) if action is not None and str(action).strip() else "unknown"
    try:
        revision = int(metadata.get("rev"))
    except (TypeError, ValueError):
        revision = 0
    if kind == "add":
        item["fromRevision"] = None
        item["toRevision"] = f"@={changelist_id}"
    elif kind == "delete":
        item["fromRevision"] = f"#{revision}" if revision > 0 else None
        item["toRevision"] = None
    elif kind == "edit":
        item["fromRevision"] = f"#{revision}" if revision > 0 else None
        item["toRevision"] = f"@={changelist_id}"
    left, right = _shelf_presence(action)
    item["leftPresent"] = left
    item["rightPresent"] = right


def _scalar_field(record: Mapping[str, Any], key: str) -> Any:
    value = record.get(key)
    if isinstance(value, (list, tuple)):
        return value[0] if len(value) == 1 else None
    return value


def _normalize_describe_records(records: Any) -> list[dict[str, Any]]:
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, list) or not all(isinstance(item, Mapping) for item in records):
        raise ValueError("p4 describe -S returned malformed metadata")
    return [dict(item) for item in records]


def _validate_pending_shelf(records: Any, changelist_id: str) -> list[dict[str, Any]]:
    """Validate that tagged describe data represents this live shelf.

    A submitted change can still contain ``depotFile`` fields, so file presence
    alone is never enough to classify it as a shelf.  Missing/ambiguous status
    is treated as an error rather than guessed.
    """
    normalized = _normalize_describe_records(records)
    if not normalized:
        raise ValueError("p4 describe -S returned no changelist record")
    requested = str(changelist_id).strip()
    for record in normalized:
        returned = _scalar_field(record, "change")
        if returned is None or str(returned).strip() != requested:
            raise ValueError(
                "p4 describe -S did not return the requested changelist record")
        status = _scalar_field(record, "status")
        if not isinstance(status, str) or status.strip().lower() != "pending":
            raise ValueError(
                "the changelist is not pending; submitted changes are not shelves")
    files = _expand_describe_files(normalized)
    if not files:
        raise ValueError("the pending changelist has no shelved files")
    return files


def _output_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, (list, tuple)):
        total = 0
        for item in value:
            if isinstance(item, str):
                total += len(item.encode("utf-8"))
            elif isinstance(item, (bytes, bytearray)):
                total += len(item)
        return total
    return 0


def _check_p4_output(p4: Any, result: Any, command: str) -> None:
    """Fail closed when P4 suppresses an error at a low exception level."""
    errors = getattr(p4, "errors", None) or []
    if errors:
        raise ValueError(f"{command} returned P4 errors: {errors}")
    messages = getattr(p4, "messages", None) or []
    severe = []
    unknown = []
    for message in messages:
        severity = getattr(message, "severity", None)
        if severity is None and isinstance(message, Mapping):
            severity = message.get("severity")
        try:
            if severity is not None and int(severity) >= 3:
                severe.append(message)
            elif severity is None:
                unknown.append(message)
        except (TypeError, ValueError):
            unknown.append(message)
    # At exception level 1, P4 may suppress a failed command into an empty
    # result with only a textual diagnostic.  Never interpret that shape as
    # an empty/clean shelf.  Non-empty output with an unclassified message is
    # retained as a warning because valid describe output commonly carries
    # informational records.
    if severe or ((result is None or result == []) and unknown):
        detail = severe or unknown
        raise ValueError(f"{command} returned P4 diagnostics: {detail}")


async def _read_revision_bytes(p4: Any, spec: str, max_bytes: int) -> bytes:
    """Read one depot/shelf revision for add/delete anchor recovery."""
    if not spec:
        raise ValueError("a revision spec is required")
    previous_tagged = getattr(p4, "tagged", True)
    try:
        p4.tagged = False
        result = p4.run("print", "-q", spec)
        _check_p4_output(p4, result, f"p4 print {spec}")
    finally:
        p4.tagged = previous_tagged
    if result is None:
        result = []
    if isinstance(result, (str, bytes, bytearray)):
        result = [result]
    if not isinstance(result, (list, tuple)):
        raise ValueError(f"p4 print {spec} returned malformed output")
    chunks: list[bytes] = []
    total = 0
    for item in result:
        if isinstance(item, (bytes, bytearray)):
            chunk = bytes(item)
        elif isinstance(item, str):
            chunk = item.encode("utf-8")
        else:
            raise ValueError(f"p4 print {spec} returned a non-text chunk")
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"revision exceeds max_bytes={max_bytes}")
        chunks.append(chunk)
    if not chunks:
        diagnostics = " ".join(str(message).lower()
                                for message in (getattr(p4, "messages", None) or []))
        if any(marker in diagnostics for marker in (
                "no such", "not found", "does not exist", "unknown file",
                "no file", "not on client", "revision does not exist")):
            raise ValueError(f"p4 print {spec} reported a missing revision")
    return b"".join(chunks)

class ShelveServices:
    """Shelve services for shelve operations"""
    
    def __init__(self, connection_manager: P4ConnectionManager):
        self.connection_manager = connection_manager

    async def list_shelves(self, user: str, limit: int = 50) -> List[Dict[str, Any]]:
        """List shelved changelists"""
        async with self.connection_manager.get_connection() as p4:
            try:
                args = ["changes", "-s", "shelved", f"-m{limit}"]
                if user:
                    args.append("-u")
                    args.append(user)
                shelves = p4.run(*args)
                return {"status": "success", "message": [{k: v for k, v in shelf.items()} for shelf in shelves]}
            except P4Exception as e:
                logger.error(f"P4Error: Failed to list shelves: {e}")
                return {"status": "error", "message": str(e)}

    async def get_shelve_diff(
            self,
            changelist_id: str,
            structured: bool = False,
            context_lines: int = 3,
            max_files: int = 200,
            max_bytes: int = 5_000_000,
        ) -> Dict[str, Any]:
        """Get a shelved changelist diff.

        The historical response (``structured=False``) is left untouched and
        remains the P4Python ``list[str]`` output.  Opting into structured mode
        switches to unified diff output and parses it into file/hunk/line
        records with explicit left/right anchors.  ``context_lines`` is passed
        to P4's ``-duN`` option and is also used when trimming/rebuilding the
        parsed hunks.
        """
        if context_lines < 0 or context_lines > 100:
            return {"status": "error", "message": "context_lines must be between 0 and 100"}
        if max_files < 1:
            return {"status": "error", "message": "max_files must be positive"}
        if max_bytes < 1:
            return {"status": "error", "message": "max_bytes must be positive"}
        async with self.connection_manager.get_connection() as p4:
            current_tag = getattr(p4, "tagged", True)
            try:
                if not structured:
                    p4.tagged = False
                    # ``-dw`` is the historical normal/ed diff contract.
                    diff = p4.run("describe", "-a", "-S", "-dw", changelist_id)
                    return {"status": "success", "message": diff}

                # Fetch the tagged file inventory separately.  describe's
                # unified output intentionally omits deletes and binary files;
                # the inventory is the authority used to mark those gaps.
                p4.tagged = True
                runner = getattr(p4, "run_describe", None)
                records = (runner("-S", str(changelist_id)) if callable(runner)
                           else p4.run("describe", "-S", str(changelist_id)))
                _check_p4_output(
                    p4, records, f"p4 describe -S {changelist_id}")
                metadata = _validate_pending_shelf(records, str(changelist_id))
                p4.tagged = False
                diff = p4.run(
                    "describe", "-a", "-S", f"-du{context_lines}", changelist_id)
                _check_p4_output(
                    p4, diff, f"p4 describe -du{context_lines} -S {changelist_id}")
                if _output_size(diff) > _MAX_STRUCTURED_DIFF_OUTPUT_BYTES:
                    raise ValueError(
                        "structured shelf diff exceeds the server response safety limit "
                        f"({_MAX_STRUCTURED_DIFF_OUTPUT_BYTES} bytes)")
                parsed = parse_unified_diff(
                    diff,
                    context_lines=context_lines,
                    metadata={"files": metadata},
                    max_bytes=max_bytes,
                )
                # Some p4d versions emit only a ``====`` header for text
                # adds/deletes (and occasionally edits) in ``describe -du``.
                # The tagged inventory tells us which side exists; read that
                # side explicitly so the structured response still contains
                # safe one-sided anchors.  Binary files remain unsupported.
                parsed_files = list(parsed.get("files") or [])
                metadata_by_path = {
                    item.get("depotFile"): item for item in metadata
                    if item.get("depotFile")
                }
                recoverable_reasons = {
                    "no unified diff section",
                    "added file section has no diff content",
                    "file section has no diff content",
                    "file section has no unified hunk",
                }
                for item in parsed_files[:max_files]:
                    if item.get("supported", False) or item.get("binary"):
                        continue
                    if item.get("hunks"):
                        continue
                    if item.get("reason") not in recoverable_reasons:
                        continue
                    path = item.get("depotFile")
                    metadata_item = metadata_by_path.get(path, {})
                    action = metadata_item.get("action", item.get("action"))
                    kind = change_kind(action)
                    if kind == "unknown" or action is None or not str(action).strip():
                        item["reason"] = "missing or unsupported file action"
                        continue
                    declared_type = str(
                        metadata_item.get("type") or item.get("type") or ""
                    ).lower()
                    if "binary" in declared_type:
                        item.update({
                            "binary": True,
                            "supported": False,
                            "complete": False,
                            "reason": "binary file; line diff unavailable",
                        })
                        continue
                    try:
                        revision = int(metadata_item.get("rev"))
                    except (TypeError, ValueError):
                        revision = 0
                    if kind == "add":
                        left_spec = None
                        right_spec = f"{path}@={changelist_id}" if path else None
                    elif kind == "delete":
                        left_spec = f"{path}#{revision}" if path and revision > 0 else None
                        right_spec = None
                    else:
                        left_spec = f"{path}#{revision}" if path and revision > 0 else None
                        right_spec = f"{path}@={changelist_id}" if path else None
                    if ((kind == "add" and right_spec is None)
                            or (kind == "delete" and left_spec is None)
                            or (kind == "edit" and (left_spec is None or right_spec is None))):
                        item["reason"] = "missing reliable revision reference for recovery"
                        continue
                    try:
                        left_bytes = await _read_revision_bytes(
                            p4, left_spec, max_bytes) if left_spec else b""
                        right_bytes = await _read_revision_bytes(
                            p4, right_spec, max_bytes) if right_spec else b""
                        if looks_binary(left_bytes, declared_type) or looks_binary(
                                right_bytes, declared_type):
                            item.update({
                                "binary": True,
                                "supported": False,
                                "complete": False,
                                "reason": "binary content detected; line diff unavailable",
                            })
                            continue
                        recovered = build_hunks(left_bytes, right_bytes, context_lines)
                        item.update(recovered)
                        item.update({
                            "action": action,
                            "type": metadata_item.get("type", item.get("type")),
                            "source": "p4-print-shelf",
                            "binary": False,
                            "supported": True,
                            "complete": True,
                            "fromRevision": f"#{revision}" if left_spec else None,
                            "toRevision": f"@={changelist_id}" if right_spec else None,
                            "recoveredFromMissingSection": True,
                        })
                        # The initial parser reason is no longer true after a
                        # successful p4 print recovery.
                        item.pop("reason", None)
                        if "utf-8-replace" in {
                                recovered.get("oldEncoding"), recovered.get("newEncoding")}:
                            item.update({
                                "supported": False,
                                "complete": False,
                                "reason": "content could not be decoded without replacement",
                                "encodingWarning": True,
                            })
                    except Exception as recovery_error:
                        item["reason"] = str(recovery_error)

                # Rebuild the parser's aggregate fields after recovery.  The
                # parser initially marked these entries as missing; retaining
                # that stale flag would report ``complete=false`` even when
                # every omitted add/delete was recovered successfully.
                recovered_missing = set(parsed.get("missingFiles") or [])
                for item in parsed_files:
                    if item.get("supported") and item.get("complete"):
                        recovered_missing.discard(item.get("depotFile"))
                if recovered_missing:
                    parsed["missingFiles"] = sorted(recovered_missing)
                else:
                    parsed.pop("missingFiles", None)
                parsed["complete"] = bool(parsed_files) and all(
                    item.get("supported", False) and item.get("complete", False)
                    for item in parsed_files
                ) and not parsed.get("limited") and not parsed.get("unexpectedFiles")
                omitted_entries = metadata[max_files:]
                files = parsed_files[:max_files]
                for item in files:
                    metadata_item = metadata_by_path.get(item.get("depotFile"), {})
                    _apply_shelf_revision_refs(
                        item, metadata_item, str(changelist_id))
                    # Keep the more specific recovery origin on entries that
                    # required p4 print; ordinary parsed sections originate
                    # from describe's unified stream.
                    item.setdefault("source", "p4-describe-shelf")
                parsed["files"] = files
                parsed.update({
                    "changelist": str(changelist_id),
                    "shelfPresent": True,
                    "contextLines": context_lines,
                    "maxFiles": max_files,
                    "maxBytes": max_bytes,
                    "complete": bool(parsed.get("complete")) and not omitted_entries,
                    "source": "p4-describe-shelf",
                })
                if omitted_entries:
                    parsed["omittedFiles"] = [
                        item.get("depotFile") for item in omitted_entries
                    ]
                unsupported = [
                    {"depotFile": item.get("depotFile"), "reason": item.get("reason")}
                    for item in files if not item.get("supported", False)
                ]
                if unsupported:
                    parsed["errors"] = unsupported
                # Raw chunks are an implementation detail.  Returning them
                # would duplicate large shelf contents in the MCP response and
                # bypass the structured output limits.
                parsed.pop("raw", None)
                return {"status": "success", "message": parsed}
            except P4Exception as e:
                logger.error(f"P4Error: Failed to get shelve diff for changelist '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}
            except Exception as e:
                logger.error(f"Failed to parse shelve diff for changelist '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}
            finally:
                p4.tagged = current_tag

    async def get_shelve_files(self, changelist_id: str) -> List[Dict[str, Any]]:
        """Get files in a shelved changelist"""
        async with self.connection_manager.get_connection() as p4:
            try:
                current_tag = p4.tagged
                p4.tagged = True
                files = p4.run_describe( "-S", changelist_id)
                _check_p4_output(
                    p4, files, f"p4 describe -S {changelist_id}")
                if not isinstance(files, list) or not all(
                        isinstance(file, Mapping) for file in files):
                    raise ValueError(
                        "p4 describe -S returned a malformed file list")
                return {"status": "success", "message": [{k: v for k, v in file.items()} for file in files]}
            except P4Exception as e:
                logger.error(f"P4Error: Failed to get shelved files for changelist '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}
            except ValueError as e:
                # ``_check_p4_output`` deliberately raises for malformed
                # tagged output and diagnostics that P4Python may suppress at
                # the configured exception level.  Keep this endpoint's
                # contract stable: callers must receive an error envelope,
                # never an uncaught parser/diagnostic exception.
                logger.error(f"Invalid shelved files response for changelist '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}
            except TypeError as e:
                logger.error(f"Malformed shelved files response for changelist '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}
            finally:
                p4.tagged = current_tag

    async def shelve_files(self, changelist_id: str, files: List[str], force: bool = False) -> Dict[str, Any]:
        """Shelve files in a changelist"""
        async with self.connection_manager.get_connection() as p4:
            try:
                if force:
                    shelved = p4.run_shelve("-f", "-c", changelist_id, *files)
                else:
                    shelved = p4.run_shelve("-c", changelist_id, *files)
                return {"status": "success", "message": shelved}
            except P4Exception as e:
                logger.error(f"P4Error: Failed to shelve files in changelist '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}

    async def unshelve_files(self, changelist_id: str, files: List[str], force: bool = False) -> Dict[str, Any]:
        """Unshelve files from a shelved changelist"""
        async with self.connection_manager.get_connection() as p4:
            try:
                if force:
                    unshelved = p4.run("unshelve", "-f", "-s", changelist_id, *files)
                else:
                    unshelved = p4.run("unshelve", "-s", changelist_id, *files)
                return {"status": "success", "message": unshelved}
            except P4Exception as e:
                logger.error(f"P4Error: Failed to unshelve files from changelist '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}

    async def delete_shelve(self, changelist_id: str, files: List[str]) -> None:
        """Delete a shelved changelist"""
        async with self.connection_manager.get_connection() as p4:
            try:
                args = ["-d", "-c", changelist_id]
                if files:
                    args.extend(files)
                result = p4.run_shelve(*args)
                return {"status": "success", "message": result}
            except P4Exception as e:
                logger.error(f"P4Error: Failed to delete shelve '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}

    async def update_shelve(self, changelist_id: str, files: List[str], force: bool = False) -> Dict[str, Any]:
        """Update a shelved changelist with new files"""
        async with self.connection_manager.get_connection() as p4:
            try:
                if force:
                    updated = p4.run_shelve("-f", "-c", changelist_id, *files)
                else:
                    updated = p4.run_shelve("-c", changelist_id, *files)
                return {"status": "success", "message": updated}
            except P4Exception as e:
                logger.error(f"P4Error: Failed to update shelve '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}

    async def unshelve_to_changelist(self, changelist_id: str, target_changelist: str) -> Dict[str, Any]:
        """Unshelve files to a specific changelist"""
        async with self.connection_manager.get_connection() as p4:
            try:
                if target_changelist == "default":
                    unshelved = p4.run("unshelve", "-s", changelist_id)
                else:
                    unshelved = p4.run("unshelve", "-s", changelist_id, "-c", target_changelist)
                return {"status": "success", "message": unshelved}
            except P4Exception as e:
                logger.error(f"P4Error: Failed to unshelve files from changelist '{changelist_id}' to '{target_changelist}': {e}")
                return {"status": "error", "message": str(e)}
