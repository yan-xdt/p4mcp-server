"""Small, dependency-free helpers for line-addressable diffs.

Swarm's ``reviews/{id}/files`` endpoint returns file metadata rather than
hunks.  These helpers turn two decoded revisions (or P4's unified output) into
a conservative representation that callers can use to place inline comments.
The module deliberately has no P4/HTTP dependency so the line mapping can be
tested in isolation.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any, Iterable, List, Mapping, Optional, Sequence


def decode_text(value: Any) -> tuple[str, str]:
    """Decode bytes and return ``(text, encoding)``.

    P4 files commonly contain UTF-8, UTF-16 (with a BOM), and legacy byte
    sequences.  BOM detection must happen before the UTF-8 attempt: UTF-16
    bytes often contain NULs and would otherwise be misclassified as binary.
    The final fallback is loss-tolerant and is reported in the encoding field
    so a caller can decide whether it is suitable for review.
    """

    if isinstance(value, str):
        return value, "str"
    raw = bytes(value or b"")

    for bom, encoding in (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
    ):
        if raw.startswith(bom):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                break

    # UTF-16 without a BOM often *does* decode as UTF-8 (with embedded NULs),
    # so checking it only after a UnicodeDecodeError misclassifies ordinary
    # source files as binary.  Require a strong alternating-NUL signal before
    # trying either endianness; arbitrary binary blobs should remain on the
    # loss-tolerant path below.
    for encoding in ("utf-16-le", "utf-16-be"):
        text = _decode_bomless_utf16(raw, encoding)
        if text is not None:
            return text, encoding
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def looks_binary(value: Any, declared_type: Optional[str] = None) -> bool:
    """Conservatively identify content for which line anchors are unsafe."""

    if declared_type and "binary" in str(declared_type).lower():
        return True
    if isinstance(value, str):
        # A decoded Python string should normally not contain NULs, but a
        # proxy may hand us a text-looking value that still carries them.
        # Treat that as binary unless it came from an explicitly recognised
        # UTF-16/32 byte sequence (which is handled before decoding).
        return "\x00" in value
    raw = bytes(value or b"")
    # UTF-16/32 text legitimately contains NUL bytes.  BOM-marked content is
    # known text; recognize the same strong no-BOM UTF-16 signal as
    # ``decode_text`` so the service-level binary gate does not reject valid
    # UTF-16 files before they reach the decoder.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return False
    if _decode_bomless_utf16(raw, "utf-16-le") is not None:
        return False
    if _decode_bomless_utf16(raw, "utf-16-be") is not None:
        return False
    return b"\x00" in raw[:8192]


def _decode_bomless_utf16(raw: bytes, encoding: str) -> Optional[str]:
    """Return decoded text for a strong no-BOM UTF-16 byte pattern.

    This deliberately requires alternating NUL bytes in a sample and a clean
    decode with no embedded NULs.  It avoids treating arbitrary binary data as
    source while keeping service-level binary checks consistent with
    :func:`decode_text`.
    """
    if len(raw) < 4 or len(raw) % 2:
        return None
    sample = raw[:512]
    even = sample[::2]
    odd = sample[1::2]
    zero_even = even.count(0) / max(1, len(even))
    zero_odd = odd.count(0) / max(1, len(odd))
    if max(zero_even, zero_odd) < 0.30 or min(zero_even, zero_odd) > 0.10:
        return None
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        return None
    return text if text and "\x00" not in text else None


def _split_lines(text: str) -> List[str]:
    """Split while retaining line endings for Swarm ``content`` payloads."""

    return text.splitlines(keepends=True)


def _line_text(line: str) -> str:
    """Return a comparison/rendering value without its line terminator."""

    return line.rstrip("\r\n")


def _range_start(index: int, count: int) -> int:
    # Unified diff convention: an empty range is anchored at the insertion
    # point (0 for a file that is entirely added).
    return index + 1 if count else index


def _append_line(
        lines: list[dict[str, Any]],
        kind: str,
        left_line: Optional[int],
        right_line: Optional[int],
        content: str,
) -> None:
    if kind == "context":
        side = "both"
    elif kind == "delete":
        side = "left"
    else:
        side = "right"
    prefix = " " if kind == "context" else ("-" if kind == "delete" else "+")
    lines.append({
        "kind": kind,
        "type": kind,
        "side": side,
        "leftLine": left_line,
        "rightLine": right_line,
        "oldLine": left_line,
        "newLine": right_line,
        # Keep the exact line ending.  Swarm's context contract expects the
        # content lines to carry their terminators when they have one.
        "content": content,
        "text": _line_text(content),
        "prefix": prefix,
        "diffLine": prefix + _line_text(content),
    })


def build_hunks(old: Any, new: Any, context_lines: int = 3) -> dict[str, Any]:
    """Build line-addressable hunks from two text values.

    Added lines have ``leftLine=None`` and deleted lines have
    ``rightLine=None``.  Comparison ignores only CR/LF spelling; the original
    target-side line ending is retained in context/add content so an inline
    comment can be sent back to Swarm without fabricating text.
    """

    if context_lines < 0 or context_lines > 100:
        raise ValueError("context_lines must be between 0 and 100")
    old_text, old_encoding = decode_text(old)
    new_text, new_encoding = decode_text(new)
    old_lines = _split_lines(old_text)
    new_lines = _split_lines(new_text)

    # A CRLF -> LF conversion is not a logical source-line change.  Matching
    # normalized keys prevents a review from becoming entirely red/green just
    # because the two P4 revisions were checked out with different EOLs.
    old_keys = [_line_text(line) for line in old_lines]
    new_keys = [_line_text(line) for line in new_lines]
    # Keep the normalization decision observable.  P4's ``diff2 -du`` can
    # report a change when only CRLF/LF spelling differs; structured review
    # anchors intentionally compare logical source lines, but callers still
    # need to know that the underlying bytes were not identical.
    old_has_final_newline = not old_lines or old_lines[-1].endswith(("\n", "\r"))
    new_has_final_newline = not new_lines or new_lines[-1].endswith(("\n", "\r"))
    logical_lines_equal = old_keys == new_keys
    line_ending_only = (
        old_text != new_text
        and logical_lines_equal
        and old_has_final_newline == new_has_final_newline
    )
    final_newline_only = (
        old_text != new_text
        and logical_lines_equal
        and old_has_final_newline != new_has_final_newline
    )
    matcher = SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    hunks: list[dict[str, Any]] = []

    for group in matcher.get_grouped_opcodes(context_lines):
        if not group:
            continue
        first, last = group[0], group[-1]
        old_start, old_end = first[1], last[2]
        new_start, new_end = first[3], last[4]
        hunk_lines: list[dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset in range(i2 - i1):
                    # Use the new side for context content; its line ending is
                    # the one that corresponds to a right-side Swarm anchor.
                    content = new_lines[j1 + offset]
                    _append_line(
                        hunk_lines, "context", i1 + offset + 1,
                        j1 + offset + 1, content,
                    )
            elif tag == "delete":
                for offset, line in enumerate(old_lines[i1:i2]):
                    _append_line(hunk_lines, "delete", i1 + offset + 1, None, line)
            elif tag == "insert":
                for offset, line in enumerate(new_lines[j1:j2]):
                    _append_line(hunk_lines, "add", None, j1 + offset + 1, line)
            elif tag == "replace":
                for offset, line in enumerate(old_lines[i1:i2]):
                    _append_line(hunk_lines, "delete", i1 + offset + 1, None, line)
                for offset, line in enumerate(new_lines[j1:j2]):
                    _append_line(hunk_lines, "add", None, j1 + offset + 1, line)

        hunk = {
            "oldStart": _range_start(old_start, old_end - old_start),
            "oldLines": old_end - old_start,
            "newStart": _range_start(new_start, new_end - new_start),
            "newLines": new_end - new_start,
            "lines": hunk_lines,
        }
        hunk["header"] = (
            f"@@ -{hunk['oldStart']},{hunk['oldLines']} "
            f"+{hunk['newStart']},{hunk['newLines']} @@"
        )
        hunks.append(hunk)

    return {
        "hunks": hunks,
        "oldLineCount": len(old_lines),
        "newLineCount": len(new_lines),
        "oldEncoding": old_encoding,
        "newEncoding": new_encoding,
        "normalized": True,
        "lineEndingOnly": line_ending_only,
        "finalNewlineOnly": final_newline_only,
        "oldHasFinalNewline": old_has_final_newline,
        "newHasFinalNewline": new_has_final_newline,
    }


_HUNK_HEADER = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+"
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@(?:\s?(?P<section>.*))?$"
)
_FILE_HEADER = re.compile(
    r"^\s*====\s+(?P<body>.+?)\s*={3,4}(?:\s*(?P<summary>.*))?$"
)
_FILE_SIDE = re.compile(
    # A diff2 side may use a depot revision (``#7``) or a change/shelf
    # revision (``@=200`` / ``@200``). Keep the reference separate from the
    # path so callers can construct an exact P4 file spec instead of treating
    # ``@=200`` as part of the depot filename.
    r"^(?P<path><none>|.+?)(?P<rev>#(?:none|\d+)|@=?\d+)?"
    r"(?:\s+\((?P<type>[^)]*)\))?\s*$",
    re.IGNORECASE,
)


def _parse_file_header(control: str) -> Optional[dict[str, Any]]:
    """Parse P4 ``describe`` and ``diff2`` file headers.

    ``describe`` emits one side (``==== path#rev (type) ====``), while
    ``diff2`` emits both sides (``==== left - right ==== summary``).  The
    latter is the only reliable way to compare two pending shelves, so both
    forms must retain their side metadata instead of treating the whole line
    as one path.
    """
    match = _FILE_HEADER.match(control)
    if not match:
        return None
    body = match.group("body").strip()
    summary = (match.group("summary") or "").strip() or None
    # A depot path may theoretically contain " - "; split only when the
    # suffix parses as a valid P4 side, preferring the rightmost separator.
    sides: Optional[tuple[str, str]] = None
    def is_side(value: str) -> bool:
        value = value.strip()
        if value.lower() == "<none>":
            return True
        # A diff2 side is a depot file reference.  Requiring the depot-root
        # prefix prevents a legal depot path containing the literal string
        # `` - `` from being mistaken for the separator between two sides.
        match = _FILE_SIDE.match(value)
        return match is not None and str(match.group("path") or "").startswith("//")
    # Prefer the rightmost valid separator.  Depot paths may contain spaces
    # (and, in a few repositories, the literal `` - `` sequence); choosing
    # the first separator can silently swap a path for a malformed side.
    separators = list(re.finditer(r"\s+-\s+", body))
    for separator in reversed(separators):
        left = body[:separator.start()].strip()
        right = body[separator.end():].strip()
        if is_side(left) and is_side(right):
            sides = (left, right)
            break
    if sides is None:
        sides = (body,)
    parsed_sides: list[dict[str, Any]] = []
    for side in sides:
        if side.strip().lower() == "<none>":
            parsed_sides.append({"path": None, "revision": None, "type": None})
            continue
        side_match = _FILE_SIDE.match(side)
        if not side_match:
            return None
        path = side_match.group("path")
        rev = side_match.group("rev")
        if path.lower() == "<none>":
            path = None
        if rev and rev.lower() in {"#none", "@none"}:
            rev = None
        parsed_sides.append({
            "path": path,
            "revision": rev,
            "type": side_match.group("type") or None,
        })
    left = parsed_sides[0]
    right = parsed_sides[1] if len(parsed_sides) > 1 else None
    # ``depotFile`` is the only path field that older callers understand.  For
    # a deletion the right side is ``<none>``, so retain the source path there;
    # for an addition the right path is the useful one.
    source_path = left.get("path")
    target_path = right.get("path") if right else source_path
    canonical_path = target_path or source_path
    display_side = right if right and right.get("path") else left
    result: dict[str, Any] = {
        "depotFile": canonical_path,
        "revision": display_side.get("revision"),
        "type": display_side.get("type"),
        "summary": summary,
        "sourcePath": source_path,
        "sourceRevision": left.get("revision"),
        "sourceType": left.get("type"),
        "targetPath": target_path,
        "targetRevision": right.get("revision") if right else left.get("revision"),
        "targetType": right.get("type") if right else left.get("type"),
        "dualPath": right is not None,
    }
    if right is not None and source_path and target_path and source_path != target_path:
        result["fromFile"] = source_path
    return result


# P4 has accumulated a few action spellings over time.  The values below all
# describe a content-bearing edit (or one of the explicit one-sided changes)
# for which line anchors can be meaningful.  An action outside this set is
# deliberately not guessed: a future server action may describe an archive,
# type-only operation, or metadata change with no stable source lines.
_ADD_ACTIONS = {
    "add", "branch", "move/add", "copy/add", "import/add",
}
_DELETE_ACTIONS = {
    "delete", "purge", "move/delete", "copy/delete",
}
_EDIT_ACTIONS = {
    "edit", "integrate", "merge", "copy", "bitcopy", "import", "refresh",
    "move", "modify", "sync",
}


def change_kind(action: Any) -> str:
    """Classify a P4/Swarm action conservatively.

    ``None``/empty is retained as ``edit`` for raw unified streams that have
    no metadata at all.  Once a server supplies a non-empty, unknown action,
    callers must treat the file as unsupported instead of fabricating a
    two-sided edit anchor.
    """
    if action is None:
        return "edit"
    value = str(action).strip().lower()
    if not value:
        return "edit"
    if value in _ADD_ACTIONS:
        return "add"
    if value in _DELETE_ACTIONS:
        return "delete"
    if value in _EDIT_ACTIONS:
        return "edit"
    return "unknown"


def _as_diff_lines(raw: Any) -> list[str]:
    """Normalize P4Python's chunks without losing chunk-boundary bytes."""

    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="replace")
    if isinstance(raw, str):
        # P4 command output can contain a CR from the source file immediately
        # followed by the CRLF inserted by the protocol/CLI layer.  A
        # ``\r\r\n`` record is one line ending, not an empty source line.
        return raw.replace("\r\r\n", "\r\n").splitlines(keepends=True)
    if not isinstance(raw, Iterable):
        raise ValueError("unified diff output must be text or an iterable of text chunks")

    # Decode a byte stream only after joining it.  UTF-8 code points can span
    # P4Python chunk boundaries; decoding each chunk independently would
    # inject replacement characters and make an otherwise safe context line
    # differ from the source sent by P4.  Text chunks retain the record-boundary
    # handling below.
    raw_items = list(raw)
    if raw_items and all(isinstance(item, (bytes, bytearray)) for item in raw_items):
        joined_bytes = b"".join(bytes(item) for item in raw_items)
        return _as_diff_lines(joined_bytes.decode("utf-8", errors="replace"))

    chunks: list[str] = []
    for chunk in raw_items:
        if isinstance(chunk, (bytes, bytearray)):
            chunk = bytes(chunk).decode("utf-8", errors="replace")
        if not isinstance(chunk, str):
            raise ValueError("unified diff output contains a non-text chunk")
        chunks.append(chunk)
    # P4Python returns a list of output chunks, not a list of logical lines.
    # Joining first is essential when a chunk boundary falls in the middle of
    # a source line or a hunk header.  ``diff2`` has one important exception:
    # its file-header record often has no trailing newline while the next
    # chunk starts with ``@@``.  Insert a separator only for an unambiguous
    # control-record boundary; blindly adding one to every chunk would turn an
    # unterminated source line into a fabricated line.
    def last_unterminated_record(value: str) -> Optional[str]:
        """Return only the final physical record when it lacks an EOL.

        P4Python may put a complete set of diff records and an unterminated
        final record in one chunk.  Looking at the whole chunk would make the
        earlier records look like a single header and can either lose a file
        boundary or fabricate one.  Restricting the inference to the final
        record keeps the repair local to the actual chunk boundary.
        """
        if not value or value.endswith(("\r", "\n")):
            return None
        return re.split(r"[\r\n]", value)[-1]

    def first_record(value: str) -> str:
        """Return the first physical record without its line terminator."""
        if not value:
            return ""
        return value.splitlines(keepends=True)[0].rstrip("\r\n")

    hunk_header_re = re.compile(
        r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@(?:\s.*)?$"
    )

    def first_record_continues_hunk_header(
            header: str, remaining_chunks: Sequence[str]) -> bool:
        """Resolve an ambiguous hunk-header chunk boundary.

        A chunk beginning with a diff prefix can be either the first body
        record or a continuation of optional section text on the same physical
        hunk-header line. Treat it as section text only when concatenating the
        first physical record still forms a valid header *and* the remaining
        body consumes exactly the header's declared old/new counts. Otherwise
        the caller keeps the conservative body-record interpretation.
        """
        tail = "".join(remaining_chunks).replace("\r\r\n", "\r\n")
        records = tail.splitlines(keepends=True)
        if not records or not records[0].endswith(("\r", "\n")):
            return False
        continued_header = header + records[0].rstrip("\r\n")
        match = _HUNK_HEADER.fullmatch(continued_header)
        if match is None:
            return False

        expected_old = int(match.group("old_count") or 1)
        expected_new = int(match.group("new_count") or 1)
        used_old = used_new = 0
        for record in records[1:]:
            control = record.rstrip("\r\n")
            if (_HUNK_HEADER.fullmatch(control)
                    or (control.startswith("====")
                        and _FILE_HEADER.fullmatch(control))):
                break
            if control == r"\ No newline at end of file":
                continue
            prefix = record[:1]
            if prefix == " ":
                used_old += 1
                used_new += 1
            elif prefix == "-":
                used_old += 1
            elif prefix == "+":
                used_new += 1
            else:
                return False
            if used_old > expected_old or used_new > expected_new:
                return False
        return used_old == expected_old and used_new == expected_new

    def current_hunk_consumes_declared_counts(parts: Sequence[str]) -> bool:
        """Whether the unfinished stream already contains one complete hunk."""
        records = "".join(parts).replace("\r\r\n", "\r\n").splitlines(
            keepends=True)
        latest_match: Optional[re.Match[str]] = None
        latest_index = -1
        for index, record in enumerate(records):
            control = record.rstrip("\r\n")
            if control.startswith("====") and _FILE_HEADER.fullmatch(control):
                latest_match = None
                latest_index = -1
                continue
            match = _HUNK_HEADER.fullmatch(control)
            if match is not None:
                latest_match = match
                latest_index = index
        if latest_match is None:
            return False
        expected_old = int(latest_match.group("old_count") or 1)
        expected_new = int(latest_match.group("new_count") or 1)
        used_old = used_new = 0
        for record in records[latest_index + 1:]:
            control = record.rstrip("\r\n")
            if control == r"\ No newline at end of file":
                continue
            prefix = record[:1]
            if prefix == " ":
                used_old += 1
                used_new += 1
            elif prefix == "-":
                used_old += 1
            elif prefix == "+":
                used_new += 1
            else:
                return False
            if used_old > expected_old or used_new > expected_new:
                return False
        return used_old == expected_old and used_new == expected_new

    joined_parts: list[str] = []
    for chunk_index, chunk in enumerate(chunks):
        if joined_parts and chunk:
            # Empty records can occur between a header and its body. They do
            # not represent a byte boundary, so inspect the latest non-empty
            # chunk instead of losing the header context.
            previous = next(
                (part for part in reversed(joined_parts) if part), "")
            previous_record = last_unterminated_record(previous)
            next_record = first_record(chunk)
            if previous_record is not None:
                # P4Python may split immediately after a hunk header.  The
                # first body record can therefore begin with `` ``, ``+`` or
                # ``-`` rather than another control record.  Insert a newline
                # only when the previous chunk is *exactly* a valid hunk
                # header; doing this for arbitrary chunks would alter an
                # unterminated source line.
                previous_control = previous_record
                if hunk_header_re.fullmatch(previous_control) \
                        and chunk.startswith((" ", "+", "-", "\\ No newline")):
                    if not first_record_continues_hunk_header(
                            previous_control, chunks[chunk_index:]):
                        joined_parts.append("\n")
                # A hunk header cannot be source content: unified source
                # records always begin with a space, '+' or '-'. Restore a
                # missing record separator when P4Python returns consecutive
                # hunks without line terminators.
                elif hunk_header_re.fullmatch(next_record) \
                        and hunk_header_re.fullmatch(previous_control):
                    joined_parts.append("\n")
                # P4Python may return a complete ``====`` record without its
                # terminating newline and put the first add-file body record
                # in the next chunk.  The body can begin with any character,
                # so checking only for ``@@``/``+``/``-`` above loses the first
                # character of paths such as ``foo``.  Recognise a complete
                # file header explicitly and restore that record boundary.
                # Only a column-zero, syntactically complete P4 file header is
                # allowed to imply a record boundary.  ``_FILE_HEADER``
                # intentionally tolerates indentation when parsing a complete
                # string, but indentation here could be a legitimate context
                # line from an unterminated hunk and must not create a phantom
                # file.
                elif (previous_control.startswith("====")
                      and _FILE_HEADER.fullmatch(previous_control)):
                    joined_parts.append("\n")
                elif (_FILE_HEADER.fullmatch(next_record)
                      and (previous_control.startswith((" ", "+", "-", "\\ No newline"))
                           or hunk_header_re.fullmatch(previous_control))
                      and previous_control not in {" ", "+", "-"}
                      and current_hunk_consumes_declared_counts(joined_parts)):
                    # A complete column-zero file header cannot be the next
                    # record of a unified hunk (source lines carry a diff
                    # prefix). A prefix-only preceding record is ambiguous:
                    # the chunk may have split immediately after that prefix,
                    # making this apparent header ordinary source content.
                    # Keep that byte stream intact instead of fabricating a
                    # boundary. Restrict all remaining inference to a
                    # preceding diff record so an un-terminated raw add-file
                    # body containing header-like text is not split
                    # speculatively.
                    joined_parts.append("\n")
        joined_parts.append(chunk)
    joined = "".join(joined_parts)
    # See the single-string branch above.  Do this after joining so a CR and
    # the following CRLF can be split across two P4Python chunks as well.
    return joined.replace("\r\r\n", "\r\n").splitlines(keepends=True)


def _diff_line(content: str, kind: str, left: Optional[int], right: Optional[int]) -> dict[str, Any]:
    prefix = " " if kind == "context" else ("-" if kind == "delete" else "+")
    return {
        "kind": kind,
        "type": kind,
        "side": "both" if kind == "context" else ("left" if kind == "delete" else "right"),
        "leftLine": left,
        "rightLine": right,
        "oldLine": left,
        "newLine": right,
        "content": content,
        "text": _line_text(content),
        "prefix": prefix,
        "diffLine": prefix + _line_text(content),
    }


def _trim_hunk_context(hunk: dict[str, Any], context_lines: int) -> list[dict[str, Any]]:
    """Trim/split a parsed hunk to the requested context count."""

    if context_lines < 0:
        raise ValueError("context_lines must be non-negative")
    source = list(hunk.get("lines") or [])
    changed = [i for i, line in enumerate(source) if line.get("kind") != "context"]
    if not changed:
        return []

    groups: list[list[int]] = [[changed[0]]]
    max_gap = 2 * context_lines
    for index in changed[1:]:
        if index - groups[-1][-1] - 1 > max_gap:
            groups.append([index])
        else:
            groups[-1].append(index)

    result: list[dict[str, Any]] = []
    for change_group in groups:
        start = max(0, change_group[0] - context_lines)
        end = min(len(source), change_group[-1] + context_lines + 1)
        selected = source[start:end]
        old_before = sum(line.get("kind") in {"context", "delete"} for line in source[:start])
        new_before = sum(line.get("kind") in {"context", "add"} for line in source[:start])
        old_count = sum(line.get("kind") in {"context", "delete"} for line in selected)
        new_count = sum(line.get("kind") in {"context", "add"} for line in selected)
        old_start = int(hunk.get("oldStart", 0)) + old_before
        new_start = int(hunk.get("newStart", 0)) + new_before
        trimmed = dict(hunk)
        trimmed.update({
            "oldStart": old_start,
            "oldLines": old_count,
            "newStart": new_start,
            "newLines": new_count,
            "lines": selected,
        })
        trimmed["sourceHeader"] = hunk.get("header")
        trimmed["header"] = (
            f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"
            + (f" {hunk['section']}" if hunk.get("section") else "")
        )
        result.append(trimmed)
    return result


def trim_hunk_context(hunks: Iterable[dict[str, Any]], context_lines: int = 3) -> list[dict[str, Any]]:
    """Return changed hunks with at most ``context_lines`` context lines."""

    if context_lines < 0:
        raise ValueError("context_lines must be non-negative")
    result: list[dict[str, Any]] = []
    for hunk in hunks:
        result.extend(_trim_hunk_context(hunk, context_lines))
    return result


def _metadata_path(entry: Mapping[str, Any]) -> Optional[str]:
    """Return the most useful depot path from a Swarm/P4 file record."""
    for key in ("depotFile", "toFile", "fromFile", "oldFile"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _metadata_entries(metadata: Any) -> tuple[Optional[list[dict[str, Any]]], bool]:
    """Normalize optional file metadata and its server-side ``limited`` flag."""
    limited = False
    value = metadata
    if isinstance(value, Mapping):
        limited_value = value.get("limited", False)
        limited = (
            limited_value if isinstance(limited_value, bool)
            else str(limited_value).strip().lower() in {"1", "true", "yes"}
        )
        value = value.get("files")
    if value is None:
        return None, limited
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("metadata must contain a list of file objects")
    return [dict(item) for item in value], limited


def _reconcile_metadata(
        parsed_files: list[dict[str, Any]],
        metadata: list[dict[str, Any]],
        *,
        limited: bool = False,
        max_bytes: Optional[int] = None,
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Join parsed sections to the authoritative file list.

    ``p4 describe -du -S`` omits files for which it cannot print a textual
    diff (notably deletes and binary files).  Returning only parsed sections is
    dangerous because callers would mistake an incomplete diff for a complete
    review.  Every metadata file therefore gets an output record; missing
    sections are explicit unsupported entries.
    """
    by_path: dict[str, list[dict[str, Any]]] = {}
    for item in parsed_files:
        path = item.get("depotFile")
        if isinstance(path, str) and path:
            by_path.setdefault(path, []).append(item)

    merged: list[dict[str, Any]] = []
    missing: list[str] = []
    for metadata_item in metadata:
        path = _metadata_path(metadata_item)
        candidates = by_path.get(path, []) if path else []
        parsed = candidates.pop(0) if candidates else None
        if path and not candidates:
            by_path.pop(path, None)
        if parsed is None:
            item = dict(metadata_item)
            item.setdefault("depotFile", path)
            binary = "binary" in str(
                metadata_item.get("type") or metadata_item.get("fileType") or ""
            ).lower()
            item.update({
                "hunks": [],
                "supported": False,
                "complete": False,
                "binary": binary,
                "reason": (
                    "binary file; line diff unavailable"
                    if binary else "no unified diff section"
                ),
            })
            merged.append(item)
            if path:
                missing.append(path)
            continue

        # Metadata is authoritative for the action/type and the path.  Start
        # with parser fields, then overlay only concrete metadata values so a
        # missing type in a P4 header cannot erase a ``binary`` declaration
        # from the authoritative review-files response.
        item = dict(parsed)
        for key, value in metadata_item.items():
            if value is not None:
                item[key] = value
        if path:
            item["depotFile"] = path
        action_value = item.get("action")
        action = str(action_value or "").lower()
        declared_types = [
            item.get("type"), item.get("fileType"),
            metadata_item.get("type"), metadata_item.get("fileType"),
        ]
        file_type = " ".join(str(value) for value in declared_types if value is not None).lower()
        # A parsed header may say ``text`` while the authoritative metadata
        # says ``binary`` (or vice versa).  Any binary declaration wins.
        item["binary"] = bool(item.get("binary")) or "binary" in file_type
        item.setdefault("supported", True)
        item.setdefault("complete", bool(item.get("supported")))
        # Do not turn a missing or unrecognised server action into a
        # line-addressable edit.  The metadata endpoint is authoritative for
        # action semantics; a future action may be type-only or otherwise
        # unsafe to comment on.  Raw parser callers without metadata retain
        # the compatibility default (``None`` means edit), but an API response
        # with an authoritative file inventory must fail closed here.
        if action_value is None or not str(action_value).strip():
            item.update({
                "supported": False,
                "complete": False,
                "reason": "missing file action",
            })
        elif change_kind(action_value) == "unknown":
            item.update({
                "supported": False,
                "complete": False,
                "reason": f"unsupported file action: {action_value}",
            })
        if max_bytes is not None and item.get("supported"):
            content_bytes = sum(
                len(str(line.get("content", "")).encode("utf-8"))
                for hunk in item.get("hunks", [])
                for line in hunk.get("lines", [])
            )
            if content_bytes > max_bytes:
                item.update({
                    "hunks": [],
                    "supported": False,
                    "complete": False,
                    "reason": f"parsed diff exceeds max_bytes={max_bytes}",
                })
        # A binary metadata declaration always wins over a textual-looking
        # section.  It is not safe to attach a line comment to it.
        if "binary" in file_type:
            item.update({
                "hunks": [],
                "binary": True,
                "supported": False,
                "complete": False,
                "reason": "binary file; line diff unavailable",
            })
        merged.append(item)

    unexpected: list[str] = []
    for leftovers in by_path.values():
        for item in leftovers:
            path = item.get("depotFile")
            if isinstance(path, str) and path:
                unexpected.append(path)
            item = dict(item)
            item.update({
                "supported": False,
                "complete": False,
                "reason": "diff section was not present in review file metadata",
            })
            merged.append(item)

    # ``limited`` means the metadata endpoint itself truncated the file list;
    # the caller cannot claim a complete review even if every returned section
    # parsed correctly.
    if limited:
        for item in merged:
            item.setdefault("limited", True)
    return merged, missing, unexpected


def parse_unified_diff(
        raw: Any,
        context_lines: int = 3,
        *,
        metadata: Any = None,
        max_bytes: Optional[int] = None,
) -> dict[str, Any]:
    """Parse P4's unified diff output into file/hunk/line records.

    P4 emits a command preamble, ``====`` file sections, and standard unified
    hunks.  Added text files are a special case: their section may contain the
    complete file body without an ``@@`` header.  Sections that cannot be
    mapped safely are marked ``supported=False``.  When ``metadata`` is
    supplied, it is treated as the authoritative file inventory and missing
    raw sections are materialized as explicit incomplete records.
    """

    if context_lines < 0 or context_lines > 100:
        raise ValueError("context_lines must be between 0 and 100")
    if max_bytes is not None and max_bytes < 1:
        raise ValueError("max_bytes must be positive")

    lines = _as_diff_lines(raw)
    # Parse metadata before consuming raw sections so an ``#1`` edit can be
    # distinguished from a genuine add.  Revision number alone is not enough:
    # a file's first depot revision may itself be an edit in unusual histories.
    metadata_entries, metadata_limited = _metadata_entries(metadata)
    metadata_by_path = {
        _metadata_path(entry): entry
        for entry in (metadata_entries or [])
        if _metadata_path(entry)
    }
    files: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    hunk: Optional[dict[str, Any]] = None
    body_lines: list[str] = []
    body_started = False
    post_hunk_invalid = False
    left_cursor = right_cursor = 0
    hunk_old_used = hunk_new_used = 0
    pending_old_header: Optional[str] = None

    def mark_invalid(reason: str) -> None:
        nonlocal current
        if current is not None:
            current["supported"] = False
            current["complete"] = False
            current.setdefault("reason", reason)

    def finish_hunk() -> None:
        nonlocal hunk, hunk_old_used, hunk_new_used
        if hunk is None:
            return
        if current is None:
            hunk = None
            hunk_old_used = hunk_new_used = 0
            return
        if (hunk_old_used != hunk["oldLines"]
                or hunk_new_used != hunk["newLines"]):
            hunk["complete"] = False
            mark_invalid("unified hunk line counts do not match its header")
        elif hunk.get("complete", True) is False:
            mark_invalid("unified hunk header is malformed")
        else:
            hunk["complete"] = True
        current.setdefault("hunks", []).append(hunk)
        hunk = None
        hunk_old_used = hunk_new_used = 0

    def finish_file(*, section_separator: bool = False) -> None:
        nonlocal current, body_lines, body_started, post_hunk_invalid
        finish_hunk()
        if current is None:
            body_lines = []
            body_started = False
            post_hunk_invalid = False
            return

        if post_hunk_invalid:
            mark_invalid("unexpected data after the declared unified hunks")

        # P4's add sections have no unified hunk; synthesize one from the raw
        # body.  When another ``====`` header follows, exactly one trailing
        # blank record is the section separator.  At EOF there is no reliable
        # reason to discard a final blank source line, so preserve it.
        valid_empty_add = False
        # ``diff2`` uses a header-only section for identical content.  Type
        # changes have no safe line anchor, so they remain explicitly
        # unsupported even though the file itself is fully described.
        if current.get("identical") and not current.get("hunks") and not current.get("binary"):
            valid_empty_add = True
        elif current.get("summary") == "types" and not current.get("hunks"):
            mark_invalid("file types differ but no line-addressable content was returned")
        elif not current.get("hunks") and not current.get("binary"):
            action_value = current.get("action")
            action = str(action_value or "").strip().lower()
            is_add = change_kind(action_value) == "add"
            if is_add and body_lines:
                added = list(body_lines)
                if section_separator and added and not _line_text(added[-1]):
                    added.pop()
                new_start = 1 if added else 0
                synthetic = {
                    "oldStart": 0,
                    "oldLines": 0,
                    "newStart": new_start,
                    "newLines": len(added),
                    "section": None,
                    "header": f"@@ -0,0 +{new_start},{len(added)} @@",
                    "lines": [
                        _diff_line(line, "add", None, index + 1)
                        for index, line in enumerate(added)
                    ],
                    "complete": True,
                }
                current["hunks"] = [synthetic]
                valid_empty_add = not added
            elif is_add:
                # A header-only add is how ``diff2`` represents a pending
                # add/delete pair.  Without reading the target side via
                # ``p4 print`` there is no content to anchor safely.
                mark_invalid("added file section has no diff content")
            elif (current.get("p4_section")
                  and current.get("revision") == "1"
                  and change_kind(action_value) not in {"delete", "unknown"}):
                # A first revision is not proof of an add: an edit history can
                # legitimately begin at #1.  Refuse the old heuristic unless
                # the metadata supplied an explicit add action.
                mark_invalid("cannot classify revision #1 without an add action")
            elif body_lines:
                mark_invalid("file section has no unified hunk")
            else:
                mark_invalid("file section has no diff content")

        depot_path = current.get("depotFile")
        if not isinstance(depot_path, str) or not depot_path.startswith("//"):
            mark_invalid("unified diff has no valid depot file header")
        current["supported"] = bool(current.get("supported", True))
        current["binary"] = bool(current.get("binary", False))
        if current["binary"]:
            # A later P4 binary marker is authoritative even when text-looking
            # hunks preceded it. Never leak those unsafe partial hunks.
            current["hunks"] = []
        has_representation = bool(current.get("hunks")) or valid_empty_add
        current["complete"] = bool(
            current["supported"]
            and has_representation
            and all(h.get("complete", False) for h in current.get("hunks", []))
        )
        files.append(current)
        current = None
        body_lines = []
        body_started = False
        post_hunk_invalid = False

    for raw_line in lines:
        control = raw_line.rstrip("\r\n")
        # A context line may legitimately contain text such as
        # ``==== //example ====``.  P4 file sections are column-zero records;
        # only recognize one while outside a unified hunk.  Without this
        # guard the parser creates a phantom file and truncates the real hunk.
        file_header = None
        if hunk is None and control.startswith("===="):
            candidate_header = _parse_file_header(control)
            # An added-file section is a raw file body, so a source line that
            # happens to look like ``==== //... ====`` must not become a new
            # phantom section.  When authoritative metadata is available,
            # only a header naming one of its files can terminate that body.
            if (candidate_header is not None
                    and current is not None
                    and current.get("p4_section")
                    and change_kind(current.get("action")) == "add"
                    and metadata_entries is not None):
                candidate_paths = {
                    entry_path for entry_path in (
                        candidate_header.get("depotFile"),
                        candidate_header.get("sourcePath"),
                        candidate_header.get("targetPath"),
                    ) if entry_path
                }
                known_paths = set(metadata_by_path)
                if not candidate_paths.intersection(known_paths):
                    candidate_header = None
            file_header = candidate_header
        if file_header:
            finish_file(section_separator=True)
            file_type = file_header.get("type") or file_header.get("targetType") \
                or file_header.get("sourceType")
            summary = str(file_header.get("summary") or "").strip().lower()
            current = {
                "depotFile": file_header.get("depotFile"),
                "revision": file_header.get("revision"),
                "type": file_type,
                "p4_section": True,
                "hunks": [],
                "supported": True,
                "binary": "binary" in (file_type or "").lower(),
            }
            # Preserve both sides for diff2 callers.  They are useful for
            # rename/add/delete diagnostics and do not disturb the historical
            # ``depotFile``/``revision`` fields.
            for key in (
                    "sourcePath", "sourceRevision", "sourceType",
                    "targetPath", "targetRevision", "targetType", "fromFile"):
                if file_header.get(key) is not None:
                    current[key] = file_header[key]
            if file_header.get("dualPath"):
                current["dualPath"] = True
            if summary:
                current["summary"] = summary
                if summary in {"identical", "types"}:
                    # diff2 emits a header but no hunk body for identical
                    # content (and for type-only differences).  That is a
                    # complete, useful result rather than a malformed file.
                    current["identical"] = summary == "identical"
                    current["complete"] = True
            metadata_hint = metadata_by_path.get(current["depotFile"])
            if metadata_hint is None and file_header.get("sourcePath"):
                metadata_hint = metadata_by_path.get(file_header["sourcePath"])
            if metadata_hint is not None:
                current["action"] = metadata_hint.get("action")
            if current["binary"]:
                mark_invalid("binary file; line diff unavailable")
            body_lines = []
            body_started = False
            post_hunk_invalid = False
            continue

        # Also accept ordinary unified headers when a caller supplies a raw
        # diff without P4's ``====`` wrapper.  The synthetic file remains
        # unsupported unless a real depot path is available.
        if current is None and control.startswith("--- "):
            pending_old_header = control[4:].split("\t", 1)[0]
            continue
        if current is None and control.startswith("+++ "):
            path = control[4:].split("\t", 1)[0]
            if path.startswith("b/"):
                path = path[2:]
            current = {
                "depotFile": path if path and path != "/dev/null" else None,
                "hunks": [],
                "supported": False,
                "binary": False,
                "p4_section": False,
                "unified_headers": True,
                "reason": "unified diff lacks a P4 depot file header",
            }
            body_lines = []
            body_started = True
            post_hunk_invalid = False
            pending_old_header = None
            continue

        if current is not None and (
            "binary files" in control.lower() or control.lower().startswith("binary ")
        ):
            finish_hunk()
            current["binary"] = True
            mark_invalid("binary file; line diff unavailable")
            continue

        # A P4 add section is a complete file body, not a unified hunk.  Its
        # contents may legitimately contain a line that looks like ``@@``;
        # interpreting that as a control header would fabricate line anchors.
        is_p4_add_body = (
            current is not None
            and current.get("p4_section")
            and (
                change_kind(current.get("action")) == "add"
            )
            and hunk is None
        )
        match = None if is_p4_add_body else _HUNK_HEADER.match(control)
        if match:
            if hunk is not None:
                finish_hunk()
            if current is None:
                current = {
                    "depotFile": None,
                    "hunks": [],
                    "supported": False,
                    "binary": False,
                    "reason": "unified diff has no depot file header",
                }
            old_count = int(match.group("old_count") or 1)
            new_count = int(match.group("new_count") or 1)
            old_start = int(match.group("old_start"))
            new_start = int(match.group("new_start"))
            hunk = {
                "oldStart": old_start,
                "oldLines": old_count,
                "newStart": new_start,
                "newLines": new_count,
                "section": match.group("section") or None,
                "header": control,
                "lines": [],
            }
            # A non-empty unified range cannot start at line zero.  Keep the
            # record so callers can inspect the server output, but mark it
            # incomplete instead of manufacturing invalid anchors.
            if (old_start == 0 and old_count != 0) or (new_start == 0 and new_count != 0):
                hunk["complete"] = False
                mark_invalid("unified hunk has a non-empty range starting at line zero")
            left_cursor, right_cursor = old_start, new_start
            hunk_old_used = hunk_new_used = 0
            body_lines = []
            body_started = True
            post_hunk_invalid = False
            if old_count == 0 and new_count == 0:
                finish_hunk()
            continue

        if hunk is None:
            if current is None:
                # Command preamble and unrelated text before the first file.
                continue
            if current.get("hunks"):
                # A blank line and the no-final-newline marker are valid
                # framing.  Any other text after a completed hunk is malformed
                # rather than silently discarded.
                if not control.strip() or control.startswith("\\ No newline at end of file"):
                    if control.startswith("\\ No newline"):
                        current["noFinalNewline"] = True
                    continue
                if control.startswith(("--- ", "+++ ")):
                    continue
                post_hunk_invalid = True
                continue
            if current.get("p4_section"):
                if not body_started:
                    # The blank record immediately after a P4 ``====`` header
                    # is framing.  Subsequent blank records are real file
                    # content and must be retained.
                    body_started = True
                    if not control.strip():
                        continue
                if control.startswith("\\ No newline at end of file"):
                    current["noFinalNewline"] = True
                    continue
                body_lines.append(raw_line)
                continue
            if not body_started:
                # The blank line immediately following a P4 section header is
                # framing; a second blank line is real add-file content.
                body_started = True
                if not control.strip():
                    continue
            if control.startswith("--- ") or control.startswith("+++ "):
                continue
            if control.startswith("\\ No newline at end of file"):
                current["noFinalNewline"] = True
                continue
            body_lines.append(raw_line)
            continue

        # A unified source line is identified by its first character.  Work on
        # raw_line so ``content`` keeps line endings and unterminated lines.
        if control.startswith("\\ No newline at end of file"):
            hunk["noFinalNewline"] = True
            continue
        if not raw_line:
            mark_invalid("empty line inside unified hunk")
            continue
        # Some P4 output paths strip the leading space from an empty context
        # line, leaving only its line terminator.  It is still a legitimate
        # context line when the hunk counts allow one.
        if control == "":
            if (hunk_old_used >= hunk["oldLines"]
                    or hunk_new_used >= hunk["newLines"]):
                mark_invalid("unified hunk contains more lines than its header")
                continue
            # Keep the raw record rather than the stripped control value.  A
            # blank context line is still a real source line, and its line
            # terminator is part of the exact content contract used by
            # Swarm inline comments (especially when the parser receives a
            # CRLF stream or a chunk containing only a newline).
            hunk["lines"].append(
                _diff_line(raw_line, "context", left_cursor, right_cursor))
            left_cursor += 1
            right_cursor += 1
            hunk_old_used += 1
            hunk_new_used += 1
        else:
            prefix, content = raw_line[0], raw_line[1:]
            if prefix == " ":
                if (hunk_old_used >= hunk["oldLines"]
                        or hunk_new_used >= hunk["newLines"]):
                    mark_invalid("unified hunk contains more lines than its header")
                    continue
                hunk["lines"].append(_diff_line(content, "context", left_cursor, right_cursor))
                left_cursor += 1
                right_cursor += 1
                hunk_old_used += 1
                hunk_new_used += 1
            elif prefix == "-":
                if hunk_old_used >= hunk["oldLines"]:
                    mark_invalid("unified hunk contains more old-side lines than its header")
                    continue
                hunk["lines"].append(_diff_line(content, "delete", left_cursor, None))
                left_cursor += 1
                hunk_old_used += 1
            elif prefix == "+":
                if hunk_new_used >= hunk["newLines"]:
                    mark_invalid("unified hunk contains more new-side lines than its header")
                    continue
                hunk["lines"].append(_diff_line(content, "add", None, right_cursor))
                right_cursor += 1
                hunk_new_used += 1
            else:
                mark_invalid("unexpected line inside unified hunk")
                continue

        if (hunk_old_used == hunk["oldLines"]
                and hunk_new_used == hunk["newLines"]):
            finish_hunk()

    finish_file(section_separator=False)
    for file_entry in files:
        file_entry["hunks"] = trim_hunk_context(file_entry.get("hunks", []), context_lines)

    missing: list[str] = []
    unexpected: list[str] = []
    if metadata_entries is not None:
        files, missing, unexpected = _reconcile_metadata(
            files, metadata_entries, limited=metadata_limited, max_bytes=max_bytes)
    elif max_bytes is not None:
        for file_entry in files:
            content_bytes = sum(
                len(str(line.get("content", "")).encode("utf-8"))
                for hunk_entry in file_entry.get("hunks", [])
                for line in hunk_entry.get("lines", [])
            )
            if content_bytes > max_bytes:
                file_entry.update({
                    "hunks": [],
                    "supported": False,
                    "complete": False,
                    "reason": f"parsed diff exceeds max_bytes={max_bytes}",
                })

    # No file section is never a complete structured diff.  A metadata-backed
    # empty file list is the one intentional exception: there is nothing to
    # represent, and the caller can still distinguish it via ``sawHunks``.
    # An empty raw stream is not proof that a change has no files: P4 can
    # suppress sections for binary/deleted files, and a failed command can
    # produce the same shape.  Callers that have independently validated an
    # intentionally empty change may override this at their own API layer;
    # the parser itself must remain fail-closed.
    no_sections_complete = False
    complete = (
        bool(files) and all(f.get("complete", False) for f in files)
        if files else no_sections_complete
    )
    if metadata_limited or missing or unexpected:
        complete = False
    result = {
        "files": files,
        "raw": lines,
        "complete": complete,
        "sawHunks": any(f.get("hunks") for f in files),
    }
    if missing:
        result["missingFiles"] = missing
    if unexpected:
        result["unexpectedFiles"] = unexpected
    if metadata_limited:
        result["limited"] = True
    return result


# Public aliases keep the helper discoverable without tying callers to one
# naming convention.
structured_diff = build_hunks
make_structured_diff = build_hunks
parse_diff = parse_unified_diff
