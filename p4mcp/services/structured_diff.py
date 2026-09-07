"""Shared paging and output-budget helpers for structured P4 diffs."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Optional, Sequence

from P4 import OutputHandler

from .review_diff import build_hunks, change_kind, looks_binary, parse_unified_diff


DEFAULT_MAX_TOTAL_BYTES = 10_000_000
MIN_MAX_TOTAL_BYTES = 65_536
_MAX_OMITTED_PATHS = 100
_MAX_ERROR_SUMMARIES = 100


class InventoryFingerprintMismatch(ValueError):
    """The caller's optimistic inventory token no longer matches."""

    def __init__(self, expected: str, actual: str):
        super().__init__(
            "expected_inventory_fingerprint does not match the current inventory; "
            "restart pagination from the first page"
        )
        self.expected = expected
        self.actual = actual


def normalize_file_filters(
        exclude_types: Optional[Sequence[str]] = None,
        exclude_globs: Optional[Sequence[str]] = None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return deterministic, validated file-filter values.

    P4 file types are case-insensitive while depot paths and glob patterns are
    kept case-sensitive.  Sorting makes equivalent filter sets produce the
    same inventory fingerprint regardless of command-line ordering.
    """
    if isinstance(exclude_types, (str, bytes)):
        raise ValueError("exclude_types must be a list of strings")
    if isinstance(exclude_globs, (str, bytes)):
        raise ValueError("exclude_globs must be a list of strings")
    types: set[str] = set()
    for raw in exclude_types or ():
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("exclude_types entries must be non-empty strings")
        types.add(raw.strip().lower())

    globs: set[str] = set()
    for raw in exclude_globs or ():
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("exclude_globs entries must be non-empty strings")
        globs.add(raw.strip())
    return tuple(sorted(types)), tuple(sorted(globs))


def filter_file_inventory(
        entries: Sequence[Mapping[str, Any]],
        exclude_types: Optional[Sequence[str]] = None,
        exclude_globs: Optional[Sequence[str]] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter metadata before paging or reading any file content.

    The first matching rule owns the exclusion count so the summary remains
    deterministic and its rule counts always add up to ``excludedFileCount``.
    ``binary`` intentionally matches P4's ``binary+l`` and ``ubinary`` forms,
    consistent with the structured diff's binary detection.
    """
    types, globs = normalize_file_filters(exclude_types, exclude_globs)
    selected: list[dict[str, Any]] = []
    excluded_by_rule: dict[str, int] = {}

    for raw in entries:
        if not isinstance(raw, Mapping):
            raise ValueError("file inventory contains a malformed entry")
        item = dict(raw)
        path = _file_path(item)
        if not path:
            raise ValueError("file inventory contains a file without a depot path")
        raw_type = str(
            item.get("type") or item.get("fileType")
            or item.get("filetype") or ""
        ).strip().lower()
        base_type = raw_type.split("+", 1)[0]

        reason: Optional[str] = None
        for excluded_type in types:
            matches = (
                "binary" in base_type if excluded_type == "binary"
                else base_type == excluded_type
            )
            if matches:
                reason = f"type:{excluded_type}"
                break
        if reason is None:
            for pattern in globs:
                if fnmatchcase(path, pattern):
                    reason = f"glob:{pattern}"
                    break

        if reason is None:
            selected.append(item)
        else:
            excluded_by_rule[reason] = excluded_by_rule.get(reason, 0) + 1

    summary = {
        "excludeTypes": list(types),
        "excludeGlobs": list(globs),
        "inputFileCount": len(entries),
        "selectedFileCount": len(selected),
        "excludedFileCount": len(entries) - len(selected),
        "excludedByRule": excluded_by_rule,
    }
    return selected, summary


def file_filter_identity(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return the filter portion that must participate in a fingerprint."""
    if not summary.get("excludeTypes") and not summary.get("excludeGlobs"):
        return {}
    return {
        "excludeTypes": list(summary.get("excludeTypes") or []),
        "excludeGlobs": list(summary.get("excludeGlobs") or []),
    }


def normalize_expected_fingerprint(expected: Optional[str]) -> Optional[str]:
    """Validate and normalize an optional SHA-256 continuation token."""
    if expected is None:
        return None
    normalized = expected.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError(
            "expected_inventory_fingerprint must be a 64-character SHA-256 hex string"
        )
    return normalized


def validate_expected_fingerprint(
        actual: str,
        expected: Optional[str],
    ) -> None:
    """Fail closed when a caller continues against a different inventory."""
    normalized = normalize_expected_fingerprint(expected)
    if normalized is None:
        return
    if normalized != actual:
        raise InventoryFingerprintMismatch(normalized, actual)


def json_size(value: Any) -> int:
    """Return the compact UTF-8 JSON size used for response budgeting."""
    return len(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode("utf-8"))


def _file_path(entry: Mapping[str, Any]) -> Optional[str]:
    for key in ("depotFile", "toFile", "fromFile", "oldFile"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True)
class DiffPagePlan:
    """A stable, exclusive-cursor page over an authoritative file inventory."""

    entries: tuple[dict[str, Any], ...]
    candidates: tuple[dict[str, Any], ...]
    paths: tuple[str, ...]
    start_index: int
    max_files: int
    after_file: Optional[str]
    inventory_fingerprint: str


def prepare_diff_page(
        entries: Sequence[Mapping[str, Any]],
        max_files: int,
        after_file: Optional[str] = None,
        inventory_identity: Optional[Mapping[str, Any]] = None,
    ) -> DiffPagePlan:
    """Validate, sort, and page file metadata before any file content is read.

    ``after_file`` must be the exact ``lastSeen`` value returned by an earlier
    page.  Failing closed when it disappears prevents a changed shelf from
    silently skipping files between requests.
    """
    normalized: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise ValueError("structured diff inventory contains a malformed file entry")
        item = dict(raw)
        path = _file_path(item)
        if not path:
            raise ValueError("structured diff inventory contains a file without a depot path")
        if path in seen:
            raise ValueError(f"structured diff inventory contains duplicate path: {path}")
        seen.add(path)
        normalized.append((path, item))
    normalized.sort(key=lambda pair: pair[0])
    paths = tuple(path for path, _ in normalized)
    ordered = tuple(item for _, item in normalized)

    start = 0
    if after_file is not None:
        if after_file not in seen:
            raise ValueError(
                "after_file is not present in the current inventory; the shelf may "
                "have changed, so restart pagination from the first page"
            )
        start = paths.index(after_file) + 1

    # Hash the complete normalized metadata row rather than a hand-picked
    # subset: range endpoints (diffFrom/diffTo), move sources, and future P4
    # fields can all change the content behind the same depot path.  The
    # caller-supplied identity binds identical-looking inventories to their
    # review/version/shelf generation as well.
    fingerprint_payload = {
        "identity": dict(inventory_identity or {}),
        "files": [
            {"depotFile": path, "metadata": item}
            for path, item in normalized
        ],
    }
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    return DiffPagePlan(
        entries=ordered,
        candidates=ordered[start:start + max_files],
        paths=paths,
        start_index=start,
        max_files=max_files,
        after_file=after_file,
        inventory_fingerprint=fingerprint,
    )


def finish_metadata_page(
        plan: DiffPagePlan,
        max_total_bytes: int,
        base: Optional[Mapping[str, Any]] = None,
        *,
        metadata_limited: bool = False,
    ) -> dict[str, Any]:
    """Build an exactly budgeted metadata-only page.

    Unlike ``StructuredDiffPage``, metadata entries have no supported/complete
    content flags.  This helper still applies the same exclusive cursor,
    fingerprint, and hard JSON-size contract without pretending that metadata
    has been content-reviewed.
    """
    if max_total_bytes < MIN_MAX_TOTAL_BYTES:
        raise ValueError(
            f"max_total_bytes must be at least {MIN_MAX_TOTAL_BYTES}"
        )

    files: list[dict[str, Any]] = []

    def render() -> dict[str, Any]:
        end = plan.start_index + len(files)
        has_more = end < len(plan.entries)
        result = dict(base or {})
        warnings = list(result.get("warnings") or [])
        if metadata_limited:
            warnings.append(
                "The source file metadata was limited; the inventory is incomplete."
            )
            result["limited"] = True
        result.update({
            "files": list(files),
            "afterFile": plan.after_file,
            "lastSeen": _file_path(files[-1]) if files else None,
            "hasMore": has_more,
            "totalFiles": len(plan.entries),
            "returnedFiles": len(files),
            "omittedFileCount": len(plan.entries) - end,
            "inventoryFingerprint": plan.inventory_fingerprint,
            "maxFiles": plan.max_files,
            "maxTotalBytes": max_total_bytes,
            "complete": not has_more and not metadata_limited,
            "warnings": list(dict.fromkeys(warnings)),
            "payloadBytes": 0,
        })
        # A few iterations converge because only the digit count can change.
        for _ in range(4):
            result["payloadBytes"] = json_size(result)
        return result

    for candidate in plan.candidates:
        files.append(dict(candidate))
        if json_size(render()) > max_total_bytes:
            files.pop()
            break

    if plan.candidates and not files:
        raise ValueError(
            "max_total_bytes is too small to return one metadata file record"
        )

    result = render()
    if json_size(result) > max_total_bytes:
        raise ValueError("metadata page exceeds max_total_bytes")
    return result


def _compact_for_page_budget(item: Mapping[str, Any], max_total_bytes: int) -> dict[str, Any]:
    compact = dict(item)
    compact["hunks"] = []
    compact.update({
        "supported": False,
        "complete": False,
        "truncated": True,
        "reason": f"file diff exceeds max_total_bytes={max_total_bytes}",
    })
    return compact


@dataclass
class StructuredDiffPage:
    """Accumulate one structured-diff page without exceeding its JSON budget."""

    plan: DiffPagePlan
    max_total_bytes: int
    files: list[dict[str, Any]] = field(default_factory=list)
    consumed: int = 0
    budget_limited: bool = False

    @property
    def _files_budget(self) -> int:
        # Reserve space for cursor fields, error summaries and the outer
        # service envelope.  ``finish`` also performs an exact final check.
        reserve = max(16_384, min(262_144, self.max_total_bytes // 20))
        return self.max_total_bytes - reserve

    def append(self, item: Mapping[str, Any]) -> bool:
        """Append one already-expanded file.

        Returns false when the caller must stop before this file.  If a single
        file cannot fit by itself, a compact unsupported record is consumed so
        pagination always makes progress instead of looping forever.
        """
        candidate = dict(item)
        if json_size({"files": [*self.files, candidate]}) <= self._files_budget:
            self.files.append(candidate)
            self.consumed += 1
            return True
        self.budget_limited = True
        if self.files:
            return False
        compact = _compact_for_page_budget(candidate, self.max_total_bytes)
        if json_size({"files": [compact]}) > self._files_budget:
            raise ValueError(
                "max_total_bytes is too small to return one compact file record"
            )
        self.files.append(compact)
        self.consumed += 1
        return True

    def _refresh_fields(self, result: dict[str, Any], metadata_limited: bool) -> None:
        end = self.plan.start_index + self.consumed
        remaining_paths = list(self.plan.paths[end:])
        has_more = end < len(self.plan.entries)
        last_seen = _file_path(self.files[-1]) if self.files else None
        page_complete = all(
            item.get("supported", False) and item.get("complete", False)
            for item in self.files
        )

        result["files"] = self.files
        result.update({
            "afterFile": self.plan.after_file,
            "lastSeen": last_seen,
            "hasMore": has_more,
            "pageComplete": page_complete,
            "totalFiles": len(self.plan.entries),
            "returnedFiles": len(self.files),
            "omittedFileCount": len(remaining_paths),
            "inventoryFingerprint": self.plan.inventory_fingerprint,
            "maxTotalBytes": self.max_total_bytes,
            "complete": page_complete and not has_more and not metadata_limited,
        })
        result.pop("omittedFiles", None)
        result.pop("omittedFilesTruncated", None)
        if remaining_paths:
            result["omittedFiles"] = remaining_paths[:_MAX_OMITTED_PATHS]
            if len(remaining_paths) > _MAX_OMITTED_PATHS:
                result["omittedFilesTruncated"] = True

        unsupported = [
            {"depotFile": _file_path(item), "reason": item.get("reason")}
            for item in self.files if not item.get("supported", False)
        ]
        result.pop("errors", None)
        result.pop("errorCount", None)
        result.pop("errorsTruncated", None)
        if unsupported:
            result["errorCount"] = len(unsupported)
            result["errors"] = unsupported[:_MAX_ERROR_SUMMARIES]
            if len(unsupported) > _MAX_ERROR_SUMMARIES:
                result["errorsTruncated"] = True

    def finish(
            self,
            base: Mapping[str, Any],
            *,
            metadata_limited: bool = False,
        ) -> dict[str, Any]:
        """Attach pagination metadata and enforce the exact final JSON limit."""
        result = dict(base)
        warnings = list(result.get("warnings") or [])
        if self.budget_limited:
            warnings.append(
                "The page stopped early because max_total_bytes was reached."
            )
        if metadata_limited:
            warnings.append("The source file metadata was limited; the diff is incomplete.")
            result["limited"] = True
        result["warnings"] = list(dict.fromkeys(warnings))

        self._refresh_fields(result, metadata_limited)
        # Reserve the final field at its maximum digit width before trimming.
        # This keeps adding the exact payloadBytes value from turning a valid
        # page into a late response-budget error.
        result["payloadBytes"] = self.max_total_bytes
        first_file_compacted = False

        def mark_budget_limited() -> None:
            self.budget_limited = True
            warning = "The page stopped early because max_total_bytes was reached."
            if warning not in result["warnings"]:
                result["warnings"].append(warning)

        # Optional duplicated summaries are the first things to trim. Only
        # after they are gone may the hard cap remove a complete file or its
        # hunks. Removed tail files are not consumed, so lastSeen resumes at
        # the correct record on the next request.
        while json_size(result) > self.max_total_bytes:
            if "omittedFiles" in result:
                result.pop("omittedFiles", None)
                result["omittedFilesTruncated"] = True
                continue
            if "errors" in result:
                result.pop("errors", None)
                if result.get("errorCount"):
                    result["errorsTruncated"] = True
                continue
            if len(self.files) > 1:
                self.files.pop()
                self.consumed -= 1
                mark_budget_limited()
                self._refresh_fields(result, metadata_limited)
                continue
            if self.files and not first_file_compacted:
                self.files[0] = _compact_for_page_budget(
                    self.files[0], self.max_total_bytes)
                first_file_compacted = True
                mark_budget_limited()
                self._refresh_fields(result, metadata_limited)
                continue
            raise ValueError(
                "max_total_bytes is too small for the structured diff metadata"
            )

        # payloadBytes includes its own field. Iterate until the digit count is
        # stable, then verify the advertised hard cap one final time.
        for _ in range(3):
            result["payloadBytes"] = json_size(result)
        if json_size(result) > self.max_total_bytes:
            raise ValueError("structured diff payload exceeded max_total_bytes")
        return result


class _BoundedOutputHandler(OutputHandler):
    """P4Python output handler that cancels before retaining excess bytes."""

    def __init__(self, max_bytes: int):
        super().__init__()
        self.max_bytes = max_bytes
        self.total = 0
        self.exceeded = False
        self.chunks: list[tuple[str, Any]] = []

    def _capture(self, kind: str, value: Any) -> int:
        if isinstance(value, str):
            size = len(value.encode("utf-8"))
        elif isinstance(value, (bytes, bytearray)):
            size = len(value)
        else:
            value = str(value)
            size = len(value.encode("utf-8"))
        if self.total + size > self.max_bytes:
            self.exceeded = True
            # OutputHandler result codes are flags. HANDLED prevents P4Python
            # from also retaining/reporting the oversized chunk while CANCEL
            # aborts the command.
            return self.HANDLED | self.CANCEL
        self.total += size
        self.chunks.append((kind, value))
        return self.HANDLED

    def outputText(self, value: Any) -> int:  # noqa: N802 - P4Python callback name
        return self._capture("text", value)

    def outputBinary(self, value: Any) -> int:  # noqa: N802 - P4Python callback name
        return self._capture("binary", value)

    def outputInfo(self, value: Any) -> int:  # noqa: N802 - P4Python callback name
        data = getattr(value, "data", value)
        return self._capture("info", data)


def run_bounded_untagged(
        p4: Any,
        args: Sequence[str],
        max_bytes: int,
        command: str,
        check_output: Callable[[Any, Any, str], None],
    ) -> list[Any]:
    """Run an untagged P4 command with a streaming output hard limit."""
    previous_tagged = getattr(p4, "tagged", True)
    previous_handler = getattr(p4, "handler", None)
    handler = _BoundedOutputHandler(max_bytes)
    result: Any = None
    try:
        p4.tagged = False
        p4.handler = handler
        try:
            result = p4.run(*args)
        except Exception:
            if not handler.exceeded:
                raise
        if handler.exceeded:
            raise ValueError(f"{command} exceeds max_bytes={max_bytes}")
        check_output(p4, result, command)
    finally:
        p4.handler = previous_handler
        p4.tagged = previous_tagged

    if handler.chunks:
        # outputText/outputBinary callbacks are arbitrary stream fragments,
        # so concatenate them exactly instead of exposing callback boundaries
        # to the diff parser. outputInfo is different: P4Python delivers a
        # complete protocol record (for diff2 this is commonly the ``====``
        # file header) without its record terminator. Restore that one known
        # boundary before joining the stream.
        normalized_chunks: list[Any] = []
        has_binary = any(kind == "binary" for kind, _ in handler.chunks)
        for kind, value in handler.chunks:
            if kind == "info":
                value = str(value)
                if not value.endswith(("\r", "\n")):
                    value += "\n"
            normalized_chunks.append(value)
        if has_binary or any(isinstance(value, (bytes, bytearray))
                             for value in normalized_chunks):
            combined: Any = b"".join(
                bytes(value) if isinstance(value, (bytes, bytearray))
                else str(value).encode("utf-8")
                for value in normalized_chunks
            )
        else:
            combined = "".join(str(value) for value in normalized_chunks)
        combined_size = (
            len(combined.encode("utf-8")) if isinstance(combined, str)
            else len(combined)
        )
        if combined_size > max_bytes:
            raise ValueError(f"{command} exceeds max_bytes={max_bytes}")
        return [combined]
    if result is None:
        return []
    if isinstance(result, (str, bytes, bytearray)):
        result = [result]
    if not isinstance(result, (list, tuple)):
        raise ValueError(f"{command} returned malformed output")
    total = 0
    normalized: list[Any] = []
    for value in result:
        if not isinstance(value, (str, bytes, bytearray)):
            raise ValueError(f"{command} returned a non-text chunk")
        total += len(value.encode("utf-8")) if isinstance(value, str) else len(value)
        if total > max_bytes:
            raise ValueError(f"{command} exceeds max_bytes={max_bytes}")
        normalized.append(value)
    return normalized


_REVISION_REF = re.compile(r"^(?:#\d+|@=?\d+)$")
_FULL_REVISION_SPEC = re.compile(
    r"^(?P<path>//.+?)(?P<revision>#\d+|@=?\d+)$"
)


def _revision_spec(path: Optional[str], reference: Any) -> Optional[str]:
    """Build one exact filespec without allowing a cross-file reference.

    Swarm normally returns either a bare numeric revision selector or a full
    depot filespec. A malformed response naming another depot file must not be
    read and then attributed to ``path`` in the structured result.
    """
    if not path or reference is None:
        return None
    ref = str(reference).strip()
    if not ref:
        return None
    if ref.startswith("//"):
        match = _FULL_REVISION_SPEC.fullmatch(ref)
        if match is None or match.group("path") != path:
            return None
        return ref
    if _REVISION_REF.fullmatch(ref):
        return f"{path}{ref}"
    if ref.isdigit():
        return f"{path}@={ref}"
    return None


async def _read_revision_bytes(
        p4: Any,
        spec: str,
        max_bytes: int,
        check_output: Callable[[Any, Any, str], None],
    ) -> bytes:
    result = run_bounded_untagged(
        p4,
        ("print", "-q", spec),
        max_bytes,
        f"p4 print {spec}",
        check_output,
    )
    if not result:
        diagnostics = " ".join(
            str(message).lower()
            for message in (getattr(p4, "messages", None) or [])
        )
        if any(marker in diagnostics for marker in (
                "no such", "not found", "does not exist", "unknown file",
                "no file", "not on client", "revision does not exist")):
            raise ValueError(f"p4 print {spec} reported a missing revision")
    chunks: list[bytes] = []
    for value in result:
        if isinstance(value, (bytes, bytearray)):
            chunks.append(bytes(value))
        elif isinstance(value, str):
            chunks.append(value.encode("utf-8"))
        else:
            raise ValueError(f"p4 print {spec} returned a non-text chunk")
    return b"".join(chunks)


async def build_structured_file(
        p4: Any,
        entry: Mapping[str, Any],
        *,
        target_pending: bool,
        to_change: str,
        from_change: Optional[str],
        effective_from: Optional[int],
        context_lines: int,
        max_bytes: int,
        check_output: Callable[[Any, Any, str], None],
    ) -> dict[str, Any]:
    """Expand exactly one file from an already-paged metadata inventory."""
    depot_file = entry.get("depotFile") or entry.get("toFile")
    old_file = entry.get("fromFile") or entry.get("oldFile") or depot_file
    action = entry.get("action")
    kind = change_kind(action)
    file_type = str(
        entry.get("type") or entry.get("fileType")
        or entry.get("filetype") or ""
    ).lower()
    item: dict[str, Any] = {
        "depotFile": depot_file,
        "fromFile": old_file if old_file != depot_file else None,
        "action": action,
        "type": entry.get("type") or entry.get("fileType"),
        "fromRevision": entry.get("diffFrom"),
        "toRevision": entry.get("diffTo"),
        "hunks": [],
        "source": "p4-print",
        "leftPresent": kind != "add" if kind != "unknown" else False,
        "rightPresent": kind != "delete" if kind != "unknown" else False,
    }

    if action is None or not str(action).strip():
        item.update({
            "supported": False,
            "complete": False,
            "reason": "missing file action",
            "leftPresent": False,
            "rightPresent": False,
        })
        return item
    if kind == "unknown":
        item.update({
            "supported": False,
            "complete": False,
            "reason": f"unsupported file action: {action}",
        })
        return item
    if not depot_file:
        item.update({
            "supported": False,
            "complete": False,
            "reason": "missing depotFile",
        })
        return item

    try:
        revision = int(entry.get("rev"))
    except (TypeError, ValueError):
        revision = 0
    target_ref = entry.get("diffTo")
    if target_ref is None:
        target_ref = (
            f"@={to_change}" if target_pending
            else (f"#{revision}" if revision > 0 else None)
        )
    base_ref = None if effective_from == 0 else entry.get("diffFrom")
    if base_ref is None and from_change:
        base_ref = f"@={from_change}"
    if kind == "edit" and base_ref is None and revision > 0:
        base_ref = (
            f"#{revision}" if target_pending
            else (f"#{revision - 1}" if revision > 1 else None)
        )
    if kind == "delete" and base_ref is None and revision > 0:
        base_ref = f"#{revision}"

    comparison_kind = kind
    if (kind == "add"
            and effective_from is not None
            and effective_from > 0
            and entry.get("diffFrom") is not None):
        # Swarm can retain the review-level ``add`` action after a file was
        # introduced in an earlier review version. An explicit diffFrom means
        # both selected versions contain the file, so this page must compare
        # those versions instead of rebuilding an empty-to-current diff.
        comparison_kind = "edit"

    if comparison_kind == "add":
        left_ref, right_ref = None, target_ref
    elif comparison_kind == "delete":
        left_ref, right_ref = base_ref, None
    else:
        left_ref, right_ref = base_ref, target_ref
    item["fromRevision"] = left_ref
    item["toRevision"] = right_ref
    item["leftPresent"] = comparison_kind != "add"
    item["rightPresent"] = comparison_kind != "delete"
    if comparison_kind != kind:
        item["comparisonKind"] = comparison_kind
    item["exactRange"] = bool(
        effective_from is not None
        and effective_from > 0
        and (entry.get("diffFrom") is not None or from_change is not None)
    )

    if "binary" in file_type:
        item.update({
            "source": "p4-metadata",
            "binary": True,
            "supported": False,
            "complete": False,
            "reason": "binary file; line diff unavailable",
        })
        return item

    left_spec = _revision_spec(old_file, left_ref)
    right_spec = _revision_spec(depot_file, right_ref)
    if ((comparison_kind == "edit" and (left_spec is None or right_spec is None))
            or (comparison_kind == "add" and right_spec is None)
            or (comparison_kind == "delete" and left_spec is None)):
        item.update({
            "supported": False,
            "complete": False,
            "reason": "missing reliable before/after revision reference",
        })
        return item

    if comparison_kind in {"add", "delete"} and entry.get("fileSize") is not None:
        try:
            if int(entry["fileSize"]) > max_bytes:
                raise ValueError(
                    f"file metadata size {entry['fileSize']} exceeds max_bytes={max_bytes}"
                )
        except (TypeError, ValueError) as exc:
            item.update({
                "supported": False,
                "complete": False,
                "reason": str(exc),
            })
            return item

    try:
        if comparison_kind == "edit":
            command = f"p4 diff2 -du{context_lines} {left_spec} {right_spec}"
            raw = run_bounded_untagged(
                p4,
                ("diff2", f"-du{context_lines}", left_spec, right_spec),
                max_bytes,
                command,
                check_output,
            )
            parse_entry = dict(entry)
            parse_entry["action"] = "edit"
            parsed = parse_unified_diff(
                raw,
                context_lines=context_lines,
                metadata={"files": [parse_entry]},
                max_bytes=max_bytes,
            )
            parsed_files = parsed.get("files") or []
            if len(parsed_files) != 1:
                raise ValueError("p4 diff2 did not return exactly one file section")
            item.update(dict(parsed_files[0]))
            item.update({
                "action": action,
                "source": "p4-diff2",
                "fromRevision": left_ref,
                "toRevision": right_ref,
                "leftPresent": True,
                "rightPresent": True,
            })
            if comparison_kind != kind:
                item["comparisonKind"] = comparison_kind
            item.pop("raw", None)
        else:
            left_bytes = await _read_revision_bytes(
                p4, left_spec, max_bytes, check_output) if left_spec else b""
            right_bytes = await _read_revision_bytes(
                p4, right_spec, max_bytes, check_output) if right_spec else b""
            if looks_binary(left_bytes, file_type) or looks_binary(
                    right_bytes, file_type):
                item.update({
                    "binary": True,
                    "supported": False,
                    "complete": False,
                    "reason": "binary content detected; line diff unavailable",
                })
                return item
            diff = build_hunks(left_bytes, right_bytes, context_lines)
            item.update(diff)
            item.update({
                "binary": False,
                "supported": True,
                "complete": True,
            })
            if "utf-8-replace" in {
                    diff.get("oldEncoding"), diff.get("newEncoding")}:
                item.update({
                    "hunks": [],
                    "supported": False,
                    "complete": False,
                    "reason": "content could not be decoded without replacement",
                    "encodingWarning": True,
                })
    except Exception as exc:
        item.update({
            "hunks": [],
            "supported": False,
            "complete": False,
            "reason": str(exc),
        })
    return item
