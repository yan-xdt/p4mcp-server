import asyncio

import pytest

from p4mcp.models.review_models import CommentContext
from p4mcp.services.review_services import (
    ReviewServices,
    _review_reference,
    _review_files_payload,
    _transition_fields,
)


def test_comment_context_keeps_exact_content_but_requires_swarm_two_sided_anchor():
    context = CommentContext(
        file="//depot/a.txt",
        leftLine=4,
        rightLine=5,
        content=["  exact indentation\n"],
    )
    assert context.content == ["  exact indentation\n"]

    # The diff representation may expose a null side, but the Swarm comment
    # transport must reject it locally instead of sending a request that the
    # server will answer with HTTP 400.
    with pytest.raises(ValueError, match="leftLine and rightLine"):
        CommentContext(file="//depot/a.txt", rightLine=5, content=["new\n"])

    # A two-sided inline context can omit content; Swarm resolves the context
    # from the supplied file/version when the caller does not provide it.
    assert CommentContext(file="//depot/a.txt", leftLine=4, rightLine=5).content is None


class _FakeP4:
    def __init__(self):
        self.tagged = True
        self.errors = []
        self.messages = []
        self.calls = []

    def run_describe(self, *args):
        assert args == ("-s", "-S", "200")
        return [{
            "change": "200",
            "status": "pending",
            "depotFile": ["//depot/a.txt"],
            "action": ["edit"],
            "type": ["text"],
            "rev": ["7"],
        }]

    def run(self, *args):
        self.calls.append(args)
        if args[0] == "diff2":
            return (
                "==== //depot/a.txt#7 (text) - //depot/a.txt@=200 (text) ====\n"
                "@@ -1,1 +1,1 @@\n-old\n+new\n"
            )
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


def test_from_zero_uses_depot_base_not_stale_diff_from():
    p4 = _FakeP4()
    service = ReviewServices(_Connection(p4))
    file_calls = []

    async def review_info(review_id, fields=None):
        return {"status": "success", "message": {"data": {"reviews": [{
            "id": review_id,
            "pending": True,
            "versions": [
                {"change": "150", "pending": True},
                {"change": "200", "pending": True},
            ],
        }]}}}

    async def review_files(review_id, from_version=None, to_version=None):
        file_calls.append((review_id, from_version, to_version))
        return {"status": "success", "message": {"files": [{
            "depotFile": "//depot/a.txt",
            "action": "edit",
            "type": "text",
            "rev": "7",
            # This deliberately conflicts with from_version=0.  The explicit
            # depot-base request must not be redirected to this stale ref.
            "diffFrom": "@=999",
            "diffTo": "@=200",
        }]}}

    service.get_review_info = review_info
    service.get_review_files = review_files

    result = asyncio.run(service.get_review_diff(
        100, from_version=0, to_version=2, context_lines=0))

    assert result["status"] == "success"
    assert file_calls == [(100, 0, 2)]
    diff2_calls = [call for call in p4.calls if call[0] == "diff2"]
    assert diff2_calls == [(
        "diff2", "-du0", "//depot/a.txt#7", "//depot/a.txt@=200")]


def test_latest_version_rejects_contradictory_pending_metadata():
    p4 = _FakeP4()
    service = ReviewServices(_Connection(p4))

    async def review_info(review_id, fields=None):
        return {"status": "success", "message": {"data": {"reviews": [{
            "id": review_id,
            # The review object says submitted while its latest version says
            # pending; neither side is safe to prefer for shelf selection.
            "pending": False,
            "versions": [
                {"change": "150", "pending": True},
                {"change": "200", "pending": True},
            ],
        }]}}}

    service.get_review_info = review_info

    result = asyncio.run(service.get_review_diff(100, to_version=2))

    assert result["status"] == "error"
    assert "contradictory" in str(result["message"])
    assert result["message"]["stage"] == "review-version"
    assert result["message"]["retryable"] is True


def test_latest_version_does_not_fallback_from_invalid_pending_metadata():
    service = ReviewServices(_Connection(_FakeP4()))

    async def review_info(review_id, fields=None):
        return {"status": "success", "message": {"data": {"reviews": [{
            "id": review_id,
            "pending": True,
            "versions": [{"change": "200", "pending": None}],
        }]}}}

    service.get_review_info = review_info

    result = asyncio.run(service.get_review_diff(100))

    assert result["status"] == "error"
    assert result["message"]["stage"] == "review-version"
    assert "invalid pending flag" in result["message"]["detail"]


def test_latest_version_can_use_top_level_pending_when_version_omits_it():
    p4 = _FakeP4()
    service = ReviewServices(_Connection(p4))

    async def review_info(review_id, fields=None):
        return {"status": "success", "message": {"data": {"reviews": [{
            "id": review_id,
            "pending": True,
            "versions": [{"change": "200"}],
        }]}}}

    service.get_review_info = review_info

    result = asyncio.run(service.get_review_diff(100, max_files=1))

    assert result["status"] == "success"
    assert result["message"]["pending"] is True


def test_default_diff_rejects_a_submitted_latest_version():
    service = ReviewServices(_Connection(_FakeP4()))

    async def review_info(review_id, fields=None):
        return {"status": "success", "message": {"data": {"reviews": [{
            "id": review_id,
            "pending": False,
            "versions": [{"change": "200", "pending": False}],
        }]}}}

    async def unexpected_files(*args, **kwargs):
        raise AssertionError("default diff must not fall back to submitted files")

    service.get_review_info = review_info
    service.get_review_files = unexpected_files

    result = asyncio.run(service.get_review_diff(100))

    assert result["status"] == "error"
    assert result["message"]["stage"] == "review-version"
    assert result["message"]["pending"] is False
    assert result["message"]["shelfPresent"] is False


def test_positive_from_version_requires_its_exact_changelist():
    service = ReviewServices(_Connection(_FakeP4()))

    async def review_info(review_id, fields=None):
        return {"status": "success", "message": {"data": {"reviews": [{
            "id": review_id,
            "pending": True,
            "versions": [
                {"pending": True},
                {"change": "200", "pending": True},
            ],
        }]}}}

    service.get_review_info = review_info

    result = asyncio.run(service.get_review_diff(
        100, from_version=1, to_version=2))

    assert result["status"] == "error"
    assert result["message"]["stage"] == "review-version"
    assert result["message"]["version"] == 1


def test_review_files_limited_flag_is_strict_but_accepts_numeric_boolean():
    files, limited = _review_files_payload({
        "data": {"files": [], "limited": 1},
    })
    assert files == []
    assert limited is True

    with pytest.raises(ValueError, match="invalid limited flag"):
        _review_files_payload({"data": {"files": [], "limited": "bogus"}})


def test_transition_fields_reject_missing_or_malformed_contract_data():
    with pytest.raises(ValueError, match="malformed transitions"):
        _transition_fields({"data": {"transitions": None}})
    with pytest.raises(ValueError, match="malformed blocked"):
        _transition_fields({"data": {"transitions": {}, "blocked": None}})


def test_review_reference_accepts_a_direct_field_limited_review_object():
    review = {"id": 100, "state": "needsReview"}

    assert _review_reference(review) is review


def test_pending_diff_pages_inventory_before_reading_content():
    p4 = _FakeP4()
    p4.run_describe = lambda *args: [{
        "change": "200",
        "status": "pending",
        "depotFile": ["//depot/c.txt", "//depot/a.txt", "//depot/b.txt"],
        "action": ["edit", "edit", "edit"],
        "type": ["text", "text", "text"],
        "rev": ["7", "7", "7"],
    }]
    service = ReviewServices(_Connection(p4))

    async def review_info(review_id, fields=None):
        return {"status": "success", "message": {"data": {"reviews": [{
            "id": review_id,
            "pending": True,
            "versions": [{"change": "200", "pending": True}],
        }]}}}

    async def unexpected_files(*args, **kwargs):
        raise AssertionError("current pending diff must use the shelf inventory")

    service.get_review_info = review_info
    service.get_review_files = unexpected_files

    first = asyncio.run(service.get_review_diff(100, max_files=1))
    assert first["status"] == "success"
    assert [item["depotFile"] for item in first["message"]["files"]] == [
        "//depot/a.txt"
    ]
    assert first["message"]["lastSeen"] == "//depot/a.txt"
    assert first["message"]["hasMore"] is True
    assert first["message"]["complete"] is False
    assert [call[0] for call in p4.calls] == ["diff2"]

    p4.calls.clear()
    second = asyncio.run(service.get_review_diff(
        100, max_files=1, after_file="//depot/a.txt"))
    assert [item["depotFile"] for item in second["message"]["files"]] == [
        "//depot/b.txt"
    ]
    assert second["message"]["inventoryFingerprint"] == first["message"]["inventoryFingerprint"]
    assert [call[0] for call in p4.calls] == ["diff2"]


def test_get_review_info_uses_independent_transitions_endpoint(monkeypatch):
    service = ReviewServices(_Connection(_FakeP4()))

    async def auth():
        return "auth"

    async def api_base():
        return "https://swarm.example/api/v11"

    service._get_auth = auth
    service._get_api_base = api_base
    calls = []

    class Response:
        ok = True
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        if url.endswith("/transitions"):
            return Response({"data": {
                "transitions": {"needsReview": "Needs Review"},
                "blocked": ["openTasks"],
            }})
        return Response({"data": {"reviews": [{
            "id": 100, "state": "needsReview",
        }]}})

    monkeypatch.setattr("p4mcp.services.review_services.requests.get", get)
    result = asyncio.run(service.get_review_info(100, include_transitions=True))

    assert result["status"] == "success"
    review = result["message"]["data"]["reviews"][0]
    assert review["transitions"] == {"needsReview": "Needs Review"}
    assert review["blocked"] == ["openTasks"]
    assert calls == [
        ("https://swarm.example/api/v11/reviews/100", None),
        ("https://swarm.example/api/v11/reviews/100/transitions", None),
    ]

    calls.clear()
    limited = asyncio.run(service.get_review_info(
        100, fields=["state"], include_transitions=True))
    limited_review = limited["message"]["data"]["reviews"][0]
    assert "id" not in limited_review
    assert limited_review["transitions"] == {"needsReview": "Needs Review"}
    assert calls == [
        ("https://swarm.example/api/v11/reviews/100", {
            "fields[]": ["state", "id"],
        }),
        ("https://swarm.example/api/v11/reviews/100/transitions", None),
    ]


def test_review_diff_requires_returned_review_identity():
    service = ReviewServices(_Connection(_FakeP4()))

    async def review_info(review_id, fields=None):
        return {"status": "success", "message": {"data": {"reviews": [{
            "pending": True,
            "versions": [{"change": "200", "pending": True}],
        }]}}}

    service.get_review_info = review_info

    result = asyncio.run(service.get_review_diff(100))

    assert result["status"] == "error"
    assert result["message"]["stage"] == "review-metadata"
    assert "requested review id" in result["message"]["detail"]
