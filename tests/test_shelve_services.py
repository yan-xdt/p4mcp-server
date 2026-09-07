import asyncio
import json

import pytest

from p4mcp.services.shelve_services import (
    ShelveServices,
    _apply_shelf_revision_refs,
    _validate_pending_shelf,
)


def _record(**overrides):
    value = {
        "change": "123",
        "status": "pending",
        "depotFile": ["//depot/a.txt"],
        "action": ["edit"],
        "type": ["text"],
        "rev": ["7"],
    }
    value.update(overrides)
    return value


def test_validate_pending_shelf_requires_matching_change_and_pending_status():
    assert _validate_pending_shelf([_record()], "123")[0]["depotFile"] == "//depot/a.txt"

    with pytest.raises(ValueError, match="requested changelist"):
        _validate_pending_shelf([_record(change="999")], "123")
    with pytest.raises(ValueError, match="not pending"):
        _validate_pending_shelf([_record(status="submitted")], "123")
    with pytest.raises(ValueError, match="no shelved files"):
        _validate_pending_shelf([_record(depotFile=[])], "123")
    with pytest.raises(ValueError, match="requested changelist"):
        _validate_pending_shelf([_record(change=None)], "123")


def test_validate_pending_shelf_broadcasts_scalar_metadata():
    records = [_record(
        depotFile=["//depot/a.txt", "//depot/b.txt"],
        action="edit", type="text", rev="7",
    )]
    files = _validate_pending_shelf(records, "123")
    assert [item["action"] for item in files] == ["edit", "edit"]
    assert [item["rev"] for item in files] == ["7", "7"]


def test_shelf_revision_refs_use_authoritative_inventory():
    item = {"depotFile": "//depot/a.txt", "revision": "7", "action": None}
    _apply_shelf_revision_refs(
        item,
        {"depotFile": "//depot/a.txt", "action": "edit", "rev": "7"},
        "123",
    )
    assert item["fromRevision"] == "#7"
    assert item["toRevision"] == "@=123"
    assert item["leftPresent"] is True
    assert item["rightPresent"] is True

    add = {"depotFile": "//depot/new.txt"}
    _apply_shelf_revision_refs(add, {"action": "add", "rev": "1"}, "123")
    assert add["fromRevision"] is None
    assert add["toRevision"] == "@=123"
    assert add["leftPresent"] is False
    assert add["rightPresent"] is True

    delete = {"depotFile": "//depot/old.txt"}
    _apply_shelf_revision_refs(delete, {"action": "delete", "rev": "2"}, "123")
    assert delete["fromRevision"] == "#2"
    assert delete["toRevision"] is None
    assert delete["leftPresent"] is True
    assert delete["rightPresent"] is False


class _FakeP4:
    def __init__(self, metadata, diff):
        self.tagged = True
        self.messages = []
        self.errors = []
        self.metadata = metadata
        self.diff = diff
        self.calls = []

    def run_describe(self, *args):
        self.calls.append(("describe", *args))
        return self.metadata

    def run(self, *args):
        self.calls.append(args)
        if args and args[0] == "diff2":
            return self.diff
        raise AssertionError(f"unexpected P4 command: {args}")


class _Connection:
    def __init__(self, p4):
        self.p4 = p4

    def get_connection(self):
        return self

    async def __aenter__(self):
        return self.p4

    async def __aexit__(self, *exc):
        return False


def test_structured_shelf_diff_returns_revision_refs_for_normal_edit():
    metadata = [_record()]
    diff = (
        "==== //depot/a.txt#7 (text) ====\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )
    service = ShelveServices(_Connection(_FakeP4(metadata, diff)))

    result = asyncio.run(
        service.get_shelve_diff("123", structured=True, context_lines=0))

    assert result["status"] == "success"
    item = result["message"]["files"][0]
    assert item["fromRevision"] == "#7"
    assert item["toRevision"] == "@=123"
    assert item["complete"] is True


def test_structured_shelf_diff_rejects_submitted_change():
    service = ShelveServices(_Connection(_FakeP4(
        [_record(status="submitted")], "")))

    result = asyncio.run(service.get_shelve_diff("123", structured=True))

    assert result["status"] == "error"
    assert "not pending" in str(result["message"])


def test_get_shelve_files_wraps_suppressed_p4_diagnostics():
    p4 = _FakeP4([], "")
    # At the connection's low exception level this diagnostic can otherwise
    # escape as a ValueError from _check_p4_output instead of an error envelope.
    p4.messages = [{"severity": 3, "text": "shelf lookup failed"}]
    service = ShelveServices(_Connection(p4))

    result = asyncio.run(service.get_shelve_files("123"))

    assert result["status"] == "error"
    assert "shelf lookup failed" in str(result["message"])
    assert p4.tagged is True


def test_structured_shelf_diff_pages_before_content_reads():
    metadata = [_record(
        depotFile=["//depot/c.txt", "//depot/a.txt", "//depot/b.txt"],
        action=["edit", "edit", "edit"],
        type=["text", "text", "text"],
        rev=["7", "7", "7"],
    )]
    p4 = _FakeP4(metadata, (
        "==== //depot/a.txt#7 (text) - //depot/a.txt@=123 (text) ====\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    ))
    service = ShelveServices(_Connection(p4))

    first = asyncio.run(service.get_shelve_diff(
        "123", structured=True, max_files=1))
    assert first["status"] == "success"
    assert [item["depotFile"] for item in first["message"]["files"]] == [
        "//depot/a.txt"
    ]
    assert first["message"]["hasMore"] is True
    assert first["message"]["lastSeen"] == "//depot/a.txt"
    assert p4.calls[0] == ("describe", "-s", "-S", "123")
    assert len([call for call in p4.calls if call[0] == "diff2"]) == 1
    assert not any(call[0] == "describe" and "-du3" in call for call in p4.calls)

    p4.calls.clear()
    second = asyncio.run(service.get_shelve_diff(
        "123", structured=True, max_files=1,
        after_file="//depot/a.txt"))
    assert [item["depotFile"] for item in second["message"]["files"]] == [
        "//depot/b.txt"
    ]


def test_structured_shelf_diff_rejects_stale_file_cursor():
    service = ShelveServices(_Connection(_FakeP4([_record()], "")))
    result = asyncio.run(service.get_shelve_diff(
        "123", structured=True, after_file="//depot/missing.txt"))

    assert result["status"] == "error"
    assert result["message"]["stage"] == "file-pagination"
    assert result["message"]["restartRequired"] is True


def test_structured_shelf_diff_clears_oversized_file_hunks():
    diff = (
        "==== //depot/a.txt#7 (text) - //depot/a.txt@=123 (text) ====\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )
    service = ShelveServices(_Connection(_FakeP4([_record()], diff)))
    result = asyncio.run(service.get_shelve_diff(
        "123", structured=True, max_bytes=1))

    assert result["status"] == "success"
    item = result["message"]["files"][0]
    assert item["supported"] is False
    assert item["hunks"] == []
    assert result["message"]["payloadBytes"] <= 10_000_000


def test_structured_shelf_diff_obeys_total_json_budget():
    body = "x" * 80_000
    diff = (
        "==== //depot/a.txt#7 (text) - //depot/a.txt@=123 (text) ====\n"
        f"@@ -1,1 +1,1 @@\n-old\n+{body}\n"
    )
    service = ShelveServices(_Connection(_FakeP4([_record()], diff)))
    result = asyncio.run(service.get_shelve_diff(
        "123", structured=True, max_bytes=200_000,
        max_total_bytes=65_536))

    assert result["status"] == "success"
    payload = result["message"]
    assert len(json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode("utf-8")) <= 65_536
    assert payload["files"][0]["hunks"] == []
    assert payload["lastSeen"] == "//depot/a.txt"
