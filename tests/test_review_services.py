import asyncio

import pytest

from p4mcp.models.review_models import CommentContext
from p4mcp.services.review_services import ReviewServices


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
        assert args == ("-S", "200")
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
