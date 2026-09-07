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
from .review_diff import change_kind
from .structured_diff import (
    DEFAULT_MAX_TOTAL_BYTES,
    MIN_MAX_TOTAL_BYTES,
    StructuredDiffPage,
    build_structured_file,
    prepare_diff_page,
)

logger = logging.getLogger(__name__)

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
            after_file: Optional[str] = None,
            max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        ) -> Dict[str, Any]:
        """Get a raw shelf diff or a bounded page of structured file hunks."""
        if context_lines < 0 or context_lines > 100:
            return {"status": "error", "message": "context_lines must be between 0 and 100"}
        if max_files < 1:
            return {"status": "error", "message": "max_files must be positive"}
        if max_bytes < 1:
            return {"status": "error", "message": "max_bytes must be positive"}
        if max_total_bytes < MIN_MAX_TOTAL_BYTES:
            return {
                "status": "error",
                "message": f"max_total_bytes must be at least {MIN_MAX_TOTAL_BYTES}",
            }

        async with self.connection_manager.get_connection() as p4:
            current_tag = getattr(p4, "tagged", True)
            try:
                if not structured:
                    p4.tagged = False
                    # Preserve the historical normal/ed diff response exactly.
                    diff = p4.run("describe", "-a", "-S", "-dw", changelist_id)
                    return {"status": "success", "message": diff}

                # -s explicitly suppresses diff content. Apply the cursor and
                # max_files to this lightweight inventory before issuing any
                # per-file diff2/print command.
                p4.tagged = True
                runner = getattr(p4, "run_describe", None)
                records = (
                    runner("-s", "-S", str(changelist_id))
                    if callable(runner)
                    else p4.run("describe", "-s", "-S", str(changelist_id))
                )
                _check_p4_output(
                    p4, records, f"p4 describe -s -S {changelist_id}")
                metadata = _validate_pending_shelf(records, str(changelist_id))
                try:
                    plan = prepare_diff_page(
                        metadata,
                        max_files,
                        after_file,
                        inventory_identity={
                            "kind": "shelf",
                            "change": str(changelist_id),
                        },
                    )
                except ValueError as exc:
                    return {
                        "status": "error",
                        "message": {
                            "stage": "file-pagination",
                            "changelist": str(changelist_id),
                            "detail": str(exc),
                            "restartRequired": after_file is not None,
                        },
                    }

                page = StructuredDiffPage(plan, max_total_bytes)
                for entry in plan.candidates:
                    item = await build_structured_file(
                        p4,
                        entry,
                        target_pending=True,
                        to_change=str(changelist_id),
                        from_change=None,
                        effective_from=None,
                        context_lines=context_lines,
                        max_bytes=max_bytes,
                        check_output=_check_p4_output,
                    )
                    if not page.append(item) or page.budget_limited:
                        break

                base = {
                    "changelist": str(changelist_id),
                    "shelfPresent": True,
                    "contextLines": context_lines,
                    "maxFiles": max_files,
                    "maxBytes": max_bytes,
                    "source": "p4-shelf-files",
                    "warnings": [],
                }
                try:
                    parsed = page.finish(base)
                except ValueError as exc:
                    return {
                        "status": "error",
                        "message": {
                            "stage": "response-budget",
                            "changelist": str(changelist_id),
                            "detail": str(exc),
                        },
                    }
                return {"status": "success", "message": parsed}
            except P4Exception as e:
                logger.error(
                    f"P4Error: Failed to get shelve diff for changelist '{changelist_id}': {e}")
                return {"status": "error", "message": str(e)}
            except Exception as e:
                logger.error(
                    f"Failed to parse shelve diff for changelist '{changelist_id}': {e}")
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
