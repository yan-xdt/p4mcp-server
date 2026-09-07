import asyncio
import json
from types import SimpleNamespace

import pytest

from P4 import OutputHandler

from p4mcp.services.structured_diff import (
    StructuredDiffPage,
    build_structured_file,
    prepare_diff_page,
    run_bounded_untagged,
)


def test_prepare_diff_page_sorts_and_uses_an_exact_exclusive_cursor():
    entries = [
        {"depotFile": "//depot/c.txt", "action": "edit", "rev": "3"},
        {"depotFile": "//depot/a.txt", "action": "edit", "rev": "1"},
        {"depotFile": "//depot/b.txt", "action": "edit", "rev": "2"},
    ]

    first = prepare_diff_page(entries, 1)
    second = prepare_diff_page(entries, 1, "//depot/a.txt")

    assert [item["depotFile"] for item in first.candidates] == ["//depot/a.txt"]
    assert [item["depotFile"] for item in second.candidates] == ["//depot/b.txt"]
    assert first.inventory_fingerprint == second.inventory_fingerprint
    with pytest.raises(ValueError, match="restart pagination"):
        prepare_diff_page(entries, 1, "//depot/missing.txt")


def test_inventory_fingerprint_includes_range_refs_and_source_identity():
    base = [{
        "depotFile": "//depot/a.txt",
        "action": "edit",
        "rev": "3",
        "diffFrom": "@=100",
        "diffTo": "@=200",
    }]
    first = prepare_diff_page(
        base, 1, inventory_identity={"reviewId": 10, "toVersion": 2})
    changed_ref = prepare_diff_page(
        [{**base[0], "diffFrom": "@=101"}], 1,
        inventory_identity={"reviewId": 10, "toVersion": 2})
    changed_identity = prepare_diff_page(
        base, 1, inventory_identity={"reviewId": 10, "toVersion": 3})

    assert first.inventory_fingerprint != changed_ref.inventory_fingerprint
    assert first.inventory_fingerprint != changed_identity.inventory_fingerprint


def test_optional_omitted_paths_are_trimmed_before_complete_file_hunks():
    first_path = "//a/000.txt"
    entries = [{"depotFile": first_path, "action": "edit", "rev": "1"}]
    entries.extend({
        "depotFile": f"//z/{index:03d}-" + ("x" * 641),
        "action": "edit",
        "rev": "1",
    } for index in range(100))
    page = StructuredDiffPage(prepare_diff_page(entries, 1), 65_536)
    hunk = {"oldStart": 1, "newStart": 1, "lines": [{"content": "ok\n"}]}
    assert page.append({
        "depotFile": first_path,
        "supported": True,
        "complete": True,
        "hunks": [hunk],
    }) is True

    result = page.finish({"warnings": []})

    assert result["files"][0]["hunks"] == [hunk]
    assert result["files"][0]["supported"] is True
    assert "omittedFiles" not in result
    assert result["omittedFilesTruncated"] is True
    assert len(json.dumps(
        result, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode("utf-8")) <= 65_536


class _StreamingP4:
    def __init__(self, chunks):
        self.chunks = chunks
        self.tagged = True
        self.handler = "original"
        self.errors = []
        self.messages = []
        self.reported = []
        self.calls = []

    def run(self, *args):
        self.calls.append(args)
        for chunk in self.chunks:
            action = self.handler.outputText(chunk)
            if not action & OutputHandler.HANDLED:
                self.reported.append(chunk)
            if action & OutputHandler.CANCEL:
                self.errors = ["Command cancelled by output handler"]
                raise RuntimeError("cancelled")
        return []


class _InfoThenTextP4(_StreamingP4):
    def run(self, *args):
        self.handler.outputInfo(SimpleNamespace(data="==== header ===="))
        self.handler.outputText("@@ body\n")
        return []


def _check_output(p4, result, command):
    if p4.errors:
        raise ValueError(f"{command}: {p4.errors}")


def test_bounded_output_handler_discards_partial_output_and_restores_p4_state():
    p4 = _StreamingP4(["1234", "5678"])

    with pytest.raises(ValueError, match="exceeds max_bytes=5"):
        run_bounded_untagged(
            p4, ("diff2", "left", "right"), 5, "p4 diff2", _check_output)

    assert p4.tagged is True
    assert p4.handler == "original"
    assert p4.reported == []


def test_bounded_output_handler_returns_complete_chunks_below_limit():
    p4 = _StreamingP4(["1234", "5678"])

    result = run_bounded_untagged(
        p4, ("diff2", "left", "right"), 8, "p4 diff2", _check_output)

    assert result == ["12345678"]
    assert p4.tagged is True
    assert p4.handler == "original"


def test_bounded_output_restores_info_record_boundary_and_coalesces_text():
    p4 = _InfoThenTextP4([])

    result = run_bounded_untagged(
        p4, ("diff2", "left", "right"), 100,
        "p4 diff2", _check_output)

    assert result == ["==== header ====\n@@ body\n"]


def test_explicit_range_compares_both_sides_of_an_earlier_add():
    path = "//depot/a.txt"
    p4 = _StreamingP4([
        f"==== {path}@=100 (text) - {path}@=200 (text) ====\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    ])
    entry = {
        "depotFile": path,
        # Swarm retains the overall review action even though the file exists
        # in both selected versions.
        "action": "add",
        "type": "text",
        "rev": "1",
        "diffFrom": "@=100",
        "diffTo": "@=200",
    }

    result = asyncio.run(build_structured_file(
        p4,
        entry,
        target_pending=True,
        to_change="200",
        from_change="100",
        effective_from=1,
        context_lines=3,
        max_bytes=10_000,
        check_output=_check_output,
    ))

    assert p4.calls == [("diff2", "-du3", f"{path}@=100", f"{path}@=200")]
    assert result["supported"] is True
    assert result["complete"] is True
    assert result["action"] == "add"
    assert result["comparisonKind"] == "edit"
    assert result["leftPresent"] is True
    assert result["rightPresent"] is True


def test_full_revision_reference_must_match_the_inventory_path():
    p4 = _StreamingP4([])
    result = asyncio.run(build_structured_file(
        p4,
        {
            "depotFile": "//depot/a.txt",
            "action": "edit",
            "type": "text",
            "rev": "2",
            "diffFrom": "//depot/other.txt#1",
            "diffTo": "//depot/a.txt#2",
        },
        target_pending=False,
        to_change="200",
        from_change="100",
        effective_from=1,
        context_lines=3,
        max_bytes=10_000,
        check_output=_check_output,
    ))

    assert p4.calls == []
    assert result["supported"] is False
    assert result["complete"] is False
    assert result["hunks"] == []
    assert result["reason"] == "missing reliable before/after revision reference"
