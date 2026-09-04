"""
P4 code review services layer

Complete implementation of Swarm API v11 Review endpoints

GET endpoints:
- list_reviews : GET /api/v11/reviews
- review_dashboard : GET /api/v11/reviews/dashboard
- get_review_transitions : GET /api/v11/reviews/{id}/transitions
- get_review_info : GET /api/v11/reviews/{id}
- get_review_files_readby : GET /api/v11/reviews/{id}/files/readby
- get_review_files : GET /api/v11/reviews/{id}/files?from={x}&to={y}
- get_review_activity : GET /api/v11/reviews/{id}/activity
- get_review_comments : GET /api/v11/reviews/{id}/comments

POST endpoints:
- create_review : POST /api/v11/reviews
- refresh_review_projects : POST /api/v11/reviews/{id}/refreshProjects
- vote_review : POST /api/v11/reviews/{id}/vote
- transition_review_state : POST /api/v11/reviews/{id}/transitions
- append_participants : POST /api/v11/reviews/{id}/participants
- add_review_comment : POST /api/v11/reviews/{id}/comments
- reply_to_comment : POST /api/v11/reviews/{id}/comments
- append_change_to_review : POST /api/v11/reviews/{id}/appendchange
- replace_review_with_change : POST /api/v11/reviews/{id}/replacewithchange
- join_review : POST /api/v11/reviews/{id}/join
- archive_inactive_reviews : POST /api/v11/reviews/archiveInactive

POST Comments endpoints:
- mark_comment_as_read : POST /api/v11/comments/{id}/read
- mark_comment_as_unread : POST /api/v11/comments/{id}/unread
- mark_all_comments_as_read : POST /api/v11/reviews/{id}/comments/read
- mark_all_comments_as_unread : POST /api/v11/reviews/{id}/comments/unread

PUT endpoints:
X - update_review_author : PUT /api/v11/reviews/{id}/author
- update_review_description : PUT /api/v11/reviews/{id}/description
- replace_participants : PUT /api/v11/reviews/{id}/participants

DELETE endpoints:
- delete_participants : DELETE /api/v11/reviews/{id}/participants
- leave_review : DELETE /api/v11/reviews/{id}/leave
- obliterate_review : DELETE /api/v11/reviews/{id}

"""

import logging
import re
from typing import List, Dict, Any, Optional, Union, Mapping
from P4 import P4Exception

import requests
from requests.auth import HTTPBasicAuth

from ..core.connection import P4ConnectionManager
from ..models.review_models import CommentContext
from .review_diff import build_hunks, change_kind, looks_binary, parse_unified_diff

logger = logging.getLogger(__name__)

# A unified diff is an intermediate P4 response, not the final MCP payload.
# Bound it before parsing so a pathological shelf cannot consume unbounded
# memory even when the caller's per-file limit is small.
_MAX_STRUCTURED_DIFF_OUTPUT_BYTES = 64 * 1024 * 1024


def _payload_data(payload: Any) -> Any:
    """Return the innermost data member of a Swarm envelope, if present."""
    value = payload
    for _ in range(4):
        if isinstance(value, dict) and isinstance(value.get("data"), (dict, list)):
            value = value["data"]
            continue
        break
    return value


def _review_from_payload(payload: Any) -> Optional[Dict[str, Any]]:
    """Extract exactly one review from all known Swarm envelope shapes.

    ``get_review_info`` normally returns ``data.reviews[0]``.  Test doubles,
    proxies, and older Swarm versions have also returned ``data.review``, a
    nested ``data`` member, a one-item list, or the review object directly.
    Never select the first element of an ambiguous list: doing so could attach
    a caller's review ID to a different review.
    """
    value = payload
    if isinstance(value, Mapping) and set(value.keys()) >= {"status", "message"}:
        value = value.get("message")
    for _ in range(6):
        if isinstance(value, Mapping):
            # Check direct review objects before looking through envelopes.
            if value.get("id") is not None and (
                    "versions" in value or "changes" in value):
                return dict(value)
            found = []
            for key in ("reviews", "review"):
                if key in value:
                    candidate = value.get(key)
                    if isinstance(candidate, list):
                        if len(candidate) != 1 or not isinstance(candidate[0], Mapping):
                            return None
                        found.append(candidate[0])
                    elif isinstance(candidate, Mapping):
                        found.append(candidate)
                    elif candidate is not None:
                        return None
            if found:
                if len(found) != 1:
                    return None
                value = found[0]
                continue
            nested = value.get("data")
            if isinstance(nested, (Mapping, list)):
                value = nested
                continue
            return None
        if isinstance(value, list):
            if len(value) != 1 or not isinstance(value[0], Mapping):
                return None
            value = value[0]
            continue
        return None
    return None


def _review_files_payload(payload: Any) -> tuple[list[dict[str, Any]], bool]:
    """Extract review file metadata and the Swarm ``limited`` indicator."""
    # Keep an outer ``limited`` flag as well as the normal data.limited form;
    # reverse proxies have emitted both shapes.
    outer_limited = False
    if isinstance(payload, Mapping):
        raw_limited = payload.get("limited")
        if isinstance(raw_limited, bool):
            outer_limited = raw_limited
        elif isinstance(raw_limited, str):
            outer_limited = raw_limited.strip().lower() in {"1", "true", "yes"}
    data = _payload_data(payload)
    limited = False
    if isinstance(data, Mapping):
        if "limited" in data:
            limited_value = data.get("limited")
            if isinstance(limited_value, bool):
                limited = limited_value
            elif isinstance(limited_value, str):
                limited = limited_value.strip().lower() in {"1", "true", "yes"}
        if "files" not in data:
            raise ValueError("Swarm response did not contain a files list")
        data = data["files"]
    if not isinstance(data, list) or not all(isinstance(entry, Mapping) for entry in data):
        raise ValueError("Swarm review files response has a malformed files list")
    return [dict(entry) for entry in data], (limited or outer_limited)


def _review_file_entries(payload: Any) -> List[Dict[str, Any]]:
    """Extract a well-formed review file list or raise on malformed data."""
    entries, _limited = _review_files_payload(payload)
    return entries


def _revision_spec(path: Optional[str], reference: Any) -> Optional[str]:
    """Combine a depot path with a Swarm/P4 revision reference."""
    if not path or reference is None:
        return None
    ref = str(reference).strip()
    if not ref:
        return None
    if ref.startswith("//"):
        return ref
    if ref.startswith(("@", "#")):
        return f"{path}{ref}"
    if ref.isdigit():
        return f"{path}@={ref}"
    return f"{path}{ref}" if ref[0] in "@#" else None


def _positive_change(record: Any) -> Optional[str]:
    if not isinstance(record, Mapping):
        return None
    value = record.get("change")
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and value != number:
        return None
    return str(number) if number > 0 and str(value).strip() == str(number) else None


def _change_kind(action: Any) -> str:
    """Compatibility wrapper for the shared conservative action classifier."""
    return change_kind(action)


def _as_bool(value: Any) -> Optional[bool]:
    """Parse the boolean spellings returned by Swarm and P4."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"1", "true", "yes"}:
            return True
        if value in {"0", "false", "no"}:
            return False
    return None


def _scalar_field(record: Mapping[str, Any], key: str) -> Any:
    """Return a scalar tagged field, rejecting ambiguous parallel values."""
    value = record.get(key)
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            return None
        return value[0]
    return value


def _describe_records(payload: Any) -> list[dict[str, Any]]:
    """Normalize a tagged ``p4 describe`` result without dropping malformed rows."""
    if isinstance(payload, Mapping):
        payload = [payload]
    if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
        raise ValueError("p4 describe -S returned malformed metadata")
    return [dict(item) for item in payload]


def _output_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(
            len(item.encode("utf-8")) if isinstance(item, str) else len(item)
            for item in value if isinstance(item, (str, bytes, bytearray))
        )
    return 0


def _expand_describe_files(records: Any) -> list[dict[str, Any]]:
    """Expand P4's parallel tagged file arrays into strict per-file records."""
    rows = _describe_records(records)
    expanded: list[dict[str, Any]] = []
    for record in rows:
        paths = record.get("depotFile", [])
        if isinstance(paths, str):
            paths = [paths]
        elif isinstance(paths, tuple):
            paths = list(paths)
        if not isinstance(paths, list):
            raise ValueError("p4 describe -S returned a malformed depotFile list")
        if not paths:
            continue
        count = len(paths)
        for key in ("action", "type", "rev", "fileSize", "digest"):
            value = record.get(key)
            if isinstance(value, (list, tuple)) and len(value) not in (0, count):
                raise ValueError(
                    f"p4 describe -S field {key!r} has {len(value)} values for {count} files")
        for index, path in enumerate(paths):
            if not isinstance(path, str) or not path:
                raise ValueError("p4 describe -S returned a malformed depotFile")
            item: dict[str, Any] = {"depotFile": path}
            for key in ("action", "type", "rev", "fileSize", "digest"):
                value = record.get(key)
                if isinstance(value, (list, tuple)):
                    if value:
                        item[key] = value[index]
                elif value is not None:
                    # Lightweight proxies sometimes collapse a uniform field
                    # to a scalar even when depotFile is an array. Broadcast
                    # that scalar to every path rather than silently making
                    # later files look action-less.
                    item[key] = value
            expanded.append(item)
    return expanded


class ReviewServices:
    """
    P4 Code Reviews REST API Client (v11)
    Covers all major review endpoints from Swarm API 2025.2
    """

    def __init__(self, connection_manager: P4ConnectionManager, verify_ssl: Union[bool, str] = True):
        """
        Args:
            connection_manager: P4 connection manager.
            verify_ssl: SSL verification for Swarm API requests.
                True – verify with default CA bundle (default).
                False – disable verification entirely.
                str – path to a custom CA certificate bundle (PEM).
        """
        self.connection_manager = connection_manager
        self.verify_ssl = verify_ssl

    async def _get_auth(self):
        """Get authentication credentials from P4 connection"""
        async with self.connection_manager.get_connection() as p4:
            try:
                username = p4.user
                ticket = p4.password
                
                if not ticket or ticket.strip() == "":
                    logger.error(f"P4Error: No valid ticket found for user '{username}'. Please run 'p4 login'.")
                    raise Exception(f"No P4 ticket found for user '{username}'. Please run 'p4 login' first.")
                
                return HTTPBasicAuth(username, ticket)
            except P4Exception as e:
                logger.error(f"P4Error: Failed to get authentication credentials: {e}")
                raise

    async def _get_api_base(self):
        async with self.connection_manager.get_connection() as p4:
            try:
                prop = p4.run("property", "-l", "-n", "P4.Swarm.URL")
                prop_dicts = [p for p in prop if isinstance(p, dict)]
                swarm_url = prop_dicts[0].get('value', None) if prop_dicts else None
                if not swarm_url:
                    raise Exception("Swarm URL not configured on the server.")
                self.api_base = f"{swarm_url.rstrip('/')}/api/v11"
                return self.api_base
            except P4Exception as e:
                logger.error(f"P4Error: Failed to get Swarm URL: {e}")
                raise

    def _handle_response(self, response):
        if response.ok:
            try:
                return response.json()
            except Exception:
                return {"message": response.text}
        else:
            raise Exception(f"HTTP {response.status_code}: {response.text}")
        
    # ============================================================================
    # GET endpoints
    # ============================================================================
    
    async def list_reviews(
            self,
            max_results: int = 50,
            after: Optional[str] = None,
            after_updated: Optional[str] = None,
            result_order: Optional[str] = None,
            projects: Optional[List[str]] = None,
            state: Optional[List[str]] = None,
            keywords: Optional[str] = None,
            keywords_fields: Optional[List[str]] = None,
            fields: Optional[List[str]] = None,
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews - List reviews with optional filters (v11 compliant)"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews"
            params = {"max": max_results}

            # pagination
            if after:
                params["after"] = after
            if after_updated:
                params["afterUpdated"] = after_updated
            if result_order:
                params["resultOrder"] = result_order

            # filters
            if projects:
                params["project[]"] = projects
            if state:
                params["state[]"] = state
            if keywords:
                params["keywords"] = keywords
            if keywords_fields:
                params["keywordsFields[]"] = keywords_fields

            # limit returned fields
            if fields:
                params["fields[]"] = fields

            r = requests.get(url, auth=auth, params=params, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to list reviews: {e}")
            return {"status": "error", "message": str(e)}

    async def review_dashboard(
            self, 
            max_results: int = 10
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews/dashboard - Get review dashboard for current user"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/dashboard"
            params = {"max": max_results}
            r = requests.get(url, auth=auth, params=params, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to get review dashboard: {e}")
            return {"status": "error", "message": str(e)}
        
    async def get_review_transitions(
            self, 
            review_id: int
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews/{id}/transitions - Get transitions and blockers for a review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/transitions"
            r = requests.get(url, auth=auth, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to get transitions for review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def get_review_info(
            self,
            review_id: int,
            fields: Optional[List[str]] = None,
            include_transitions: bool = False,
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews/{id} - Get information about a review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}"
            params = {}

            if fields:
                params["fields[]"] = fields
            if include_transitions:
                params["transitions"] = "true"

            r = requests.get(url, auth=auth, params=params if params else None, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to get review info for '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def get_review_files_readby(
            self, 
            review_id: int
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews/{id}/files/readby - Get read status of review files"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/files/readby"
            r = requests.get(url, auth=auth, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to get review files readby for '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def get_review_files(
            self,
            review_id: int,
            from_version: Optional[int] = None,
            to_version: Optional[int] = None
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews/{id}/files?from={x}&to={y}
        Get list of files that changed between specified versions of a review.
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/files"
            params = {}

            if from_version is not None:
                params["from"] = from_version
            if to_version is not None:
                params["to"] = to_version

            r = requests.get(url, auth=auth, params=params if params else None, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to get review files for '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    @staticmethod
    def _p4_message_severity(message: Any) -> Optional[int]:
        """Best-effort extraction of a P4 message severity."""
        severity = getattr(message, "severity", None)
        if severity is None and isinstance(message, Mapping):
            severity = message.get("severity")
        if severity is None:
            match = re.search(r"(?:Sev|severity)\s*[:=]\s*(\d+)", str(message))
            severity = match.group(1) if match else None
        try:
            return int(severity) if severity is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _check_p4_output(cls, p4, result: Any, command: str) -> None:
        """Raise when P4 returned an error that exception_level suppressed.

        P4Python can return ``[]`` while placing a warning/error in
        ``messages`` when the connection uses a low exception level.  Treating
        that as an empty file would produce a false, line-addressable diff.
        """
        errors = getattr(p4, "errors", None) or []
        if errors:
            raise ValueError(f"{command} returned P4 errors: {errors}")
        messages = getattr(p4, "messages", None) or []
        # P4Python severities follow E_INFO/E_WARN/E_FAILED/E_FATAL.  This
        # server runs with exception_level=1, and normal successful commands
        # (including ``describe``) leave severity-1 informational records in
        # ``messages``.  Only E_FAILED (3) and E_FATAL (4) are command errors.
        severe = []
        unknown = []
        for message in messages:
            severity = cls._p4_message_severity(message)
            if severity is not None and severity >= 3:
                severe.append(message)
            elif severity is None:
                unknown.append(message)
        # An unclassified diagnostic accompanying an empty result is unsafe to
        # ignore.  Non-empty output with an unclassified message is retained as
        # a warning by the caller and does not make valid file content fail.
        if severe or ((result is None or result == []) and unknown):
            detail = severe or unknown
            raise ValueError(f"{command} returned P4 diagnostics: {detail}")

    async def _read_revision_bytes(self, p4, spec: str, max_bytes: int) -> bytes:
        """Read one depot revision without allowing unbounded or malformed output."""
        if not spec:
            raise ValueError("a revision spec is required")
        previous_tagged = getattr(p4, "tagged", True)
        result = None
        try:
            p4.tagged = False
            result = p4.run("print", "-q", spec)
            self._check_p4_output(p4, result, f"p4 print {spec}")
        finally:
            p4.tagged = previous_tagged

        if result is None:
            result = []
        if isinstance(result, (str, bytes, bytearray)):
            result = [result]
        if not isinstance(result, (list, tuple)):
            raise ValueError(f"p4 print {spec} returned a malformed result")

        if not result:
            # An actually empty text revision is valid, so do not reject every
            # empty response.  P4 commonly reports a missing revision as a
            # severity-2 diagnostic (which is intentionally non-throwing at
            # this connection's exception level); inspect that diagnostic
            # before accepting an empty byte stream.
            diagnostics = getattr(p4, "messages", None) or []
            diagnostic_text = " ".join(str(message).lower() for message in diagnostics)
            if any(marker in diagnostic_text for marker in (
                    "no such", "not found", "does not exist", "unknown file",
                    "no file", "revision does not exist")):
                raise ValueError(f"p4 print {spec} reported a missing revision")

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
        return b"".join(chunks)

    @staticmethod
    def _expand_review_file_entries(payload: Any) -> tuple[list[dict[str, Any]], bool]:
        return _review_files_payload(payload)

    @staticmethod
    def _p4_describe_unified(
            p4, changelist_id: str, context_lines: int = 3) -> Any:
        """Read the authoritative diff of a pending shelf."""
        previous_tagged = getattr(p4, "tagged", True)
        try:
            p4.tagged = False
            raw = p4.run(
                "describe", "-a", "-S", f"-du{context_lines}",
                str(changelist_id),
            )
            ReviewServices._check_p4_output(
                p4, raw, f"p4 describe -du{context_lines} -S {changelist_id}")
            return raw
        finally:
            p4.tagged = previous_tagged

    @staticmethod
    def _p4_describe_shelf(p4, changelist_id: str) -> list[dict[str, Any]]:
        """Read and normalize the tagged pending-shelf record for a CL."""
        previous_tagged = getattr(p4, "tagged", True)
        try:
            p4.tagged = True
            runner = getattr(p4, "run_describe", None)
            if callable(runner):
                result = runner("-S", str(changelist_id))
            else:
                result = p4.run("describe", "-S", str(changelist_id))
            ReviewServices._check_p4_output(
                p4, result, f"p4 describe -S {changelist_id}")
            records = _describe_records(result)
            if not records:
                raise ValueError("p4 describe -S returned no changelist record")
            requested = str(changelist_id).strip()
            for record in records:
                returned = _scalar_field(record, "change")
                if returned is None or str(returned).strip() != requested:
                    raise ValueError(
                        "p4 describe -S did not return the requested changelist record")
                status = _scalar_field(record, "status")
                if not isinstance(status, str) or status.strip().lower() != "pending":
                    raise ValueError(
                        "the selected changelist is not pending; it is not a live shelf")
            files = _expand_describe_files(records)
            if not files:
                raise ValueError("the pending changelist has no shelved files")
            return files
        finally:
            p4.tagged = previous_tagged

    @staticmethod
    def _p4_diff2_unified(
            p4, left_spec: str, right_spec: str, context_lines: int = 3) -> Any:
        """Read a two-sided unified diff while preserving diff2 headers."""
        previous_tagged = getattr(p4, "tagged", True)
        try:
            p4.tagged = False
            raw = p4.run("diff2", f"-du{context_lines}", left_spec, right_spec)
            ReviewServices._check_p4_output(
                p4, raw,
                f"p4 diff2 -du{context_lines} {left_spec} {right_spec}")
            return raw
        finally:
            p4.tagged = previous_tagged

    @staticmethod
    def _presence_for_action(action: Any) -> tuple[bool, bool]:
        if action is None or not str(action).strip():
            return False, False
        kind = _change_kind(action)
        if kind == "unknown":
            return False, False
        return kind != "add", kind != "delete"

    @staticmethod
    def _diff_error(review_id: int, detail: Any, **extra: Any) -> Dict[str, Any]:
        message: Dict[str, Any] = {"review_id": review_id, "detail": str(detail)}
        message.update(extra)
        return {"status": "error", "message": message}

    async def get_review_diff(
            self,
            review_id: int,
            from_version: Optional[int] = None,
            to_version: Optional[int] = None,
            context_lines: int = 3,
            max_files: int = 200,
            max_bytes: int = 5_000_000,
        ) -> Dict[str, Any]:
        """Return line-addressable hunks for a review version range.

        ``get_review_files`` remains the backwards-compatible metadata API.
        This opt-in method uses its ``diffFrom``/``diffTo`` references when
        available and otherwise derives a safe base from the selected review
        version.  Binary files and files over ``max_bytes`` are represented as
        explicit unsupported entries; they are never decoded as source text.
        """
        if context_lines < 0 or context_lines > 100:
            return {"status": "error", "message": "context_lines must be between 0 and 100"}
        if max_files < 1:
            return {"status": "error", "message": "max_files must be positive"}
        if max_bytes < 1:
            return {"status": "error", "message": "max_bytes must be positive"}

        info_result = await self.get_review_info(
            review_id, fields=["id", "versions", "pending", "state"])
        if not isinstance(info_result, Mapping):
            return self._diff_error(
                review_id,
                "Swarm review metadata response has an invalid envelope",
                stage="review-metadata",
                retryable=True,
            )
        if info_result.get("status") != "success":
            return info_result
        review = _review_from_payload(info_result)
        if not review:
            return self._diff_error(
                review_id, "Swarm response did not contain a review object")
        returned_id = review.get("id")
        if returned_id is not None and str(returned_id).strip() != str(review_id).strip():
            return self._diff_error(
                review_id,
                "Swarm returned metadata for a different review",
                stage="review-metadata",
                retryable=False,
            )
        versions = review.get("versions")
        if not isinstance(versions, list) or not versions:
            return self._diff_error(
                review_id, "Review has no versions; cannot establish diff revisions")

        total_versions = len(versions)
        effective_to = to_version if to_version is not None else total_versions
        effective_from = from_version
        if effective_to < 1 or effective_to > total_versions:
            return self._diff_error(
                review_id, f"to_version must be between 1 and {total_versions}")
        if effective_from is not None and (effective_from < 0 or effective_from >= effective_to):
            return self._diff_error(
                review_id, "from_version must be >= 0 and less than to_version")

        to_record = versions[effective_to - 1]
        if not isinstance(to_record, Mapping):
            return self._diff_error(
                review_id, "Selected review version is malformed",
                stage="review-version", version=effective_to, retryable=False)
        from_record = versions[effective_from - 1] if effective_from else None
        if effective_from and not isinstance(from_record, Mapping):
            return self._diff_error(
                review_id, "The starting review version is malformed",
                stage="review-version", version=effective_from, retryable=False)
        to_change = _positive_change(to_record)
        from_change = _positive_change(from_record)
        if not to_change:
            return self._diff_error(
                review_id, "Selected review version has no usable changelist",
                stage="review-version", version=effective_to, retryable=False)

        # Never infer a missing pending flag as ``false``.  The distinction is
        # safety-critical: a submitted change may expose depotFile metadata but
        # cannot be used as a live review shelf.  For the latest version only,
        # Swarm's top-level flag is an acceptable fallback when the per-version
        # field was omitted by a field-limited response.
        version_pending_value = _as_bool(to_record.get("pending"))
        review_pending_value = (
            _as_bool(review.get("pending"))
            if effective_to == total_versions else None
        )
        # For the latest version both fields describe the same Swarm state.
        # A contradiction is not safe to resolve by preference: choosing the
        # top-level flag could make us read a submitted depot revision as a
        # live shelf, while choosing the version flag could hide a just-
        # submitted review.  Stop and require fresh, consistent metadata.
        if (version_pending_value is not None
                and review_pending_value is not None
                and version_pending_value != review_pending_value):
            return self._diff_error(
                review_id,
                "Review pending metadata is contradictory between the selected "
                "latest version and the review object",
                stage="review-version", version=effective_to,
                pendingKnown=False, retryable=True,
            )
        target_pending_value = version_pending_value
        if target_pending_value is None and effective_to == total_versions:
            target_pending_value = review_pending_value
        if target_pending_value is None:
            return self._diff_error(
                review_id,
                "Selected review version has no reliable pending flag",
                stage="review-version", version=effective_to,
                pendingKnown=False, retryable=False,
            )
        target_pending = target_pending_value is True

        files_result = await self.get_review_files(
            review_id,
            from_version=from_version,
            to_version=to_version,
        )
        if not isinstance(files_result, Mapping):
            return self._diff_error(
                review_id,
                "Swarm review files response has an invalid envelope",
                stage="review-files",
                retryable=True,
            )
        if files_result.get("status") != "success":
            return files_result
        try:
            entries, metadata_limited = self._expand_review_file_entries(
                files_result.get("message"))
        except (TypeError, ValueError) as exc:
            return self._diff_error(review_id, exc, stage="review-files")

        # A non-pending or explicitly ranged review must have a concrete file
        # inventory from Swarm.  An empty response can otherwise look like a
        # successful no-op and conceal an endpoint/permission failure.  The
        # default pending path is allowed to proceed because it obtains a
        # second, authoritative inventory from ``p4 describe -S`` below.
        if not entries and not (target_pending and effective_from is None):
            return self._diff_error(
                review_id,
                "Swarm returned an empty review file list; refusing to claim a complete diff",
                stage="review-files", fromVersion=effective_from,
                toVersion=effective_to, complete=False, retryable=True)

        warnings: list[str] = []
        errors: list[dict[str, Any]] = []
        omitted = max(0, len(entries) - max_files)
        if omitted:
            warnings.append(f"{omitted} file(s) omitted because max_files={max_files}")
        entries_to_expand = entries[:max_files]
        expanded: list[dict[str, Any]] = []
        parsed_complete = True
        shelf_present = False
        # In the default pending path P4's tagged shelf inventory is the
        # authoritative file list. Keep its omitted tail separately from the
        # Swarm HTTP inventory, which may be empty or limited.
        omitted_paths: list[str] = []

        # For a default pending-review diff, ``describe -du -S`` is the
        # authoritative shelf-vs-base representation and also gives useful
        # framing for deletes/binaries.  When the caller explicitly requests a
        # version range, use each file's ``diffFrom``/``diffTo`` refs (or the
        # selected version CLs) and read both revisions directly.  On this
        # server ``path@=CL`` reliably distinguishes two pending shelves; the
        # old implementation incorrectly substituted the latest shelf-vs-base
        # diff even when ``from_version`` was supplied.
        if target_pending and effective_from is None:
            try:
                async with self.connection_manager.get_connection() as p4:
                    # ``review.files`` is not a shelf-presence proof.  Confirm
                    # the selected CL is still pending and has concrete
                    # shelved files before interpreting an empty ``describe``
                    # response as a clean diff.
                    shelf_entries = self._p4_describe_shelf(p4, to_change)
                    raw = self._p4_describe_unified(p4, to_change, context_lines)
                    if _output_size(raw) > _MAX_STRUCTURED_DIFF_OUTPUT_BYTES:
                        raise ValueError(
                            "structured review diff exceeds the server response safety "
                            f"limit ({_MAX_STRUCTURED_DIFF_OUTPUT_BYTES} bytes)")
                    parsed = parse_unified_diff(
                        raw,
                        context_lines=context_lines,
                        metadata={"files": shelf_entries},
                        max_bytes=max_bytes,
                    )
                    # ``p4 describe -du -S`` omits text adds/deletes on some
                    # server versions.  Read the one existing side to recover
                    # safe one-sided anchors instead of silently reporting an
                    # incomplete diff for newly added/removed source files.
                    shelf_by_path = {
                        entry.get("depotFile"): entry for entry in shelf_entries
                        if entry.get("depotFile")
                    }
                    # Recovery reads depot content one file at a time.  Keep
                    # that work within the caller's expansion budget; files
                    # beyond ``max_files`` are represented as omitted rather
                    # than silently consuming additional P4 reads.
                    recoverable_reasons = {
                        "no unified diff section",
                        "added file section has no diff content",
                        "file section has no diff content",
                        "file section has no unified hunk",
                    }
                    for parsed_item in (parsed.get("files", []) or [])[:max_files]:
                        if parsed_item.get("supported", False) or parsed_item.get("binary"):
                            continue
                        if parsed_item.get("hunks"):
                            continue
                        if parsed_item.get("reason") not in recoverable_reasons:
                            continue
                        action = parsed_item.get("action")
                        if action is None or not str(action).strip():
                            # A revision number alone cannot distinguish an
                            # edit from an add/delete. Never synthesize an
                            # anchor when the authoritative inventory omitted
                            # the action.
                            parsed_item["reason"] = "missing file action"
                            continue
                        kind = _change_kind(action)
                        if kind == "unknown":
                            continue
                        metadata_entry = shelf_by_path.get(parsed_item.get("depotFile"), {})
                        path = parsed_item.get("depotFile")
                        try:
                            revision = int(metadata_entry.get("rev"))
                        except (TypeError, ValueError):
                            revision = 0
                        if kind == "add":
                            left_spec = None
                            right_spec = _revision_spec(path, f"@={to_change}")
                        elif kind == "delete":
                            left_spec = _revision_spec(
                                path, f"#{revision}" if revision > 0 else None)
                            right_spec = None
                        else:
                            left_spec = _revision_spec(
                                path, f"#{revision}" if revision > 0 else None)
                            right_spec = _revision_spec(path, f"@={to_change}")
                        if ((kind == "add" and right_spec is None)
                                or (kind == "delete" and left_spec is None)
                                or (kind == "edit" and (left_spec is None or right_spec is None))):
                            continue
                        try:
                            left_bytes = await self._read_revision_bytes(
                                p4, left_spec, max_bytes) if left_spec else b""
                            right_bytes = await self._read_revision_bytes(
                                p4, right_spec, max_bytes) if right_spec else b""
                            declared_type = str(
                                metadata_entry.get("type") or "").lower()
                            if looks_binary(left_bytes, declared_type) or looks_binary(right_bytes, declared_type):
                                parsed_item.update({
                                    "binary": True,
                                    "supported": False,
                                    "complete": False,
                                    "reason": "binary content detected; line diff unavailable",
                                })
                                continue
                            recovered = build_hunks(left_bytes, right_bytes, context_lines)
                            parsed_item.update(recovered)
                            parsed_item.update({
                                "source": "p4-print-shelf",
                                "binary": False,
                                "supported": True,
                                "complete": True,
                                "fromRevision": f"#{revision}" if left_spec else None,
                                "toRevision": f"@={to_change}" if right_spec else None,
                                "recoveredFromMissingSection": True,
                            })
                            # The parser's original reason described the
                            # missing unified section.  Once recovery succeeds
                            # it must not remain alongside a complete hunk.
                            parsed_item.pop("reason", None)
                            if "utf-8-replace" in {
                                    recovered.get("oldEncoding"), recovered.get("newEncoding")}:
                                parsed_item.update({
                                    "supported": False, "complete": False,
                                    "reason": "content could not be decoded without replacement",
                                })
                        except Exception as recovery_error:
                            parsed_item["reason"] = str(recovery_error)
                if not parsed.get("files"):
                    return self._diff_error(
                        review_id,
                        "The pending shelf returned no line-addressable file sections",
                        stage="p4-describe-shelf", sourceChange=to_change,
                        fromVersion=effective_from, toVersion=effective_to,
                        shelfPresent=True, complete=False, retryable=True,
                    )
                # The P4 inventory is authoritative for a pending shelf.  It
                # may contain more files than the Swarm metadata response (or
                # the caller's max_files cap), so compute omission from this
                # validated inventory as well as from the HTTP response.
                omitted = max(omitted, max(0, len(shelf_entries) - max_files))
                if omitted:
                    if not any("omitted because max_files" in w for w in warnings):
                        warnings.append(
                            f"{omitted} file(s) omitted because max_files={max_files}")
                    omitted_paths = [
                        entry.get("depotFile") for entry in shelf_entries[max_files:]
                        if entry.get("depotFile")
                    ]
                expanded = parsed.get("files", [])[:max_files]
                # Recovery may turn a parser-level "missing section" into a
                # complete one-sided add/delete.  Recompute from the actual
                # returned records rather than trusting the parser's stale
                # aggregate flag.
                parsed_complete = bool(expanded) and all(
                    item.get("supported", False)
                    and item.get("complete", False)
                    for item in expanded
                )
                if parsed.get("limited") or parsed.get("unexpectedFiles"):
                    parsed_complete = False
                shelf_present = True
                for item in expanded:
                    item.setdefault("source", "p4-describe-shelf")
                    # The textual describe stream does not always carry the
                    # exact before/after refs. Fill them from the validated
                    # pending-shelf inventory so every file remains tied to
                    # the selected latest review version.
                    metadata_entry = shelf_by_path.get(item.get("depotFile"), {})
                    action = item.get("action") or metadata_entry.get("action")
                    kind = (_change_kind(action)
                            if action is not None and str(action).strip()
                            else "unknown")
                    if action is not None and str(action).strip():
                        item.setdefault("action", action)
                    if metadata_entry.get("type") is not None:
                        item.setdefault("type", metadata_entry.get("type"))
                    try:
                        revision = int(metadata_entry.get("rev"))
                    except (TypeError, ValueError):
                        revision = 0
                    if kind == "add":
                        item.setdefault("fromRevision", None)
                        item.setdefault("toRevision", f"@={to_change}")
                    elif kind == "delete":
                        if revision > 0:
                            item.setdefault("fromRevision", f"#{revision}")
                        item.setdefault("toRevision", None)
                    elif kind == "edit":
                        if revision > 0:
                            item.setdefault("fromRevision", f"#{revision}")
                        item.setdefault("toRevision", f"@={to_change}")
                    left_present, right_present = self._presence_for_action(
                        item.get("action"))
                    item.setdefault("leftPresent", left_present)
                    item.setdefault("rightPresent", right_present)
                    if not item.get("supported", False):
                        errors.append(item)
            except Exception as exc:
                # A failed P4 command is different from a successfully read
                # binary/unsupported file.  Returning ``status=success`` with
                # synthetic empty hunks would let a caller mistake an
                # infrastructure outage for a clean review, so fail closed
                # and make the retry boundary explicit.
                return self._diff_error(
                    review_id,
                    f"Could not read pending shelf diff: {exc}",
                    stage="p4-describe-shelf",
                    sourceChange=to_change,
                    fromVersion=effective_from,
                    toVersion=effective_to,
                    retryable=True,
                )
        else:
            async with self.connection_manager.get_connection() as p4:
                # An explicit range ending in a pending version still relies
                # on a live shelf for its right-hand revision.  Validate it
                # before issuing per-file diff commands; otherwise a missing
                # shelf can be reported as a collection of misleading empty
                # files.  For submitted versions this check is intentionally
                # skipped because the right side is a depot revision.
                if target_pending:
                    try:
                        self._p4_describe_shelf(p4, to_change)
                    except Exception as exc:
                        return self._diff_error(
                            review_id,
                            f"Could not verify the selected pending shelf: {exc}",
                            stage="p4-describe-shelf",
                            sourceChange=to_change,
                            fromVersion=effective_from,
                            toVersion=effective_to,
                            shelfPresent=False,
                            retryable=True,
                        )
                    shelf_present = True
                for entry in entries_to_expand:
                    depot_file = entry.get("depotFile") or entry.get("toFile")
                    old_file = entry.get("fromFile") or entry.get("oldFile") or depot_file
                    action = entry.get("action")
                    kind = _change_kind(action)
                    file_type = str(
                        entry.get("type") or entry.get("fileType") or entry.get("filetype") or ""
                    ).lower()
                    item: dict[str, Any] = {
                        "depotFile": depot_file,
                        "fromFile": old_file if old_file != depot_file else None,
                        "action": action,
                        "type": entry.get("type"),
                        "fromRevision": entry.get("diffFrom"),
                        "toRevision": entry.get("diffTo"),
                        "hunks": [],
                        "source": "p4-print",
                        "leftPresent": kind != "add",
                        "rightPresent": kind != "delete",
                    }

                    if "binary" in file_type:
                        item.update({"binary": True, "supported": False,
                                     "complete": False,
                                     "reason": "binary file; line diff unavailable"})
                        errors.append(item)
                        expanded.append(item)
                        continue
                    if action is None or not str(action).strip():
                        item.update({
                            "supported": False,
                            "complete": False,
                            "reason": "missing file action",
                            "leftPresent": False,
                            "rightPresent": False,
                        })
                        errors.append(item)
                        expanded.append(item)
                        continue
                    if action is not None and str(action).strip() \
                            and kind == "unknown":
                        item.update({
                            "supported": False,
                            "complete": False,
                            "reason": f"unsupported file action: {action}",
                        })
                        errors.append(item)
                        expanded.append(item)
                        continue
                    if not depot_file:
                        item.update({"supported": False, "complete": False,
                                     "reason": "missing depotFile"})
                        errors.append(item)
                        expanded.append(item)
                        continue

                    # Swarm supplies exact ``@=CL`` refs for a non-zero
                    # version range.  For a full/post-commit diff, derive the
                    # adjacent depot revision from ``rev`` when the endpoint
                    # omits those refs.  A delete's old side is ``#rev`` (the
                    # last existing revision).  For a pending shelf, an edit
                    # also uses ``#rev`` because that is the depot base and
                    # ``@=to_change`` is the shelf.  Only a submitted
                    # revision falls back to ``#(rev-1)``.
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
                    # ``from_version=0`` explicitly means the depot base.  A
                    # stale/incorrect ``diffFrom`` returned by a proxy must
                    # not silently turn that request into a version-to-version
                    # diff.  For a positive starting version, retain Swarm's
                    # precise diffFrom reference when available.
                    base_ref = (
                        None if effective_from == 0
                        else entry.get("diffFrom")
                    )
                    if base_ref is None and from_change:
                        base_ref = f"@={from_change}"
                    if kind == "edit" and base_ref is None and revision > 0:
                        base_ref = (
                            f"#{revision}" if target_pending else
                            (f"#{revision - 1}" if revision > 1 else None)
                        )
                    if kind == "delete" and base_ref is None and revision > 0:
                        base_ref = f"#{revision}"

                    if kind == "add":
                        left_ref = None
                        right_ref = target_ref
                    elif kind == "delete":
                        left_ref = base_ref
                        right_ref = None
                    else:
                        left_ref = base_ref
                        right_ref = target_ref

                    item["fromRevision"] = left_ref
                    item["toRevision"] = right_ref
                    item["exactRange"] = bool(
                        effective_from is not None
                        and effective_from > 0
                        and (entry.get("diffFrom") is not None
                             or from_change is not None)
                    )

                    left_spec = _revision_spec(old_file, left_ref)
                    right_spec = _revision_spec(depot_file, right_ref)
                    size_hint = entry.get("fileSize")
                    try:
                        if size_hint is not None and int(size_hint) > max_bytes:
                            raise ValueError(
                                f"file metadata size {size_hint} exceeds max_bytes={max_bytes}")
                    except (TypeError, ValueError) as exc:
                        item.update({"supported": False, "complete": False,
                                     "reason": str(exc)})
                        errors.append(item)
                        warnings.append(f"{depot_file}: {exc}")
                        expanded.append(item)
                        continue

                    if ((kind == "edit" and (left_spec is None or right_spec is None))
                            or (kind == "add" and right_spec is None)
                            or (kind == "delete" and left_spec is None)):
                        item.update({
                            "supported": False, "complete": False,
                            "reason": "missing reliable before/after revision reference",
                        })
                        errors.append(item)
                        expanded.append(item)
                        continue
                    try:
                        if kind == "edit":
                            # For an edit, ask the server for a real diff2
                            # result.  Besides avoiding an unnecessary pair of
                            # full ``print`` calls, this preserves the two
                            # depot paths and the server's identical/types
                            # summary in the structured record.
                            raw = self._p4_diff2_unified(
                                p4, left_spec, right_spec, context_lines)
                            if _output_size(raw) > _MAX_STRUCTURED_DIFF_OUTPUT_BYTES:
                                raise ValueError(
                                    "structured file diff exceeds the server response safety "
                                    f"limit ({_MAX_STRUCTURED_DIFF_OUTPUT_BYTES} bytes)")
                            parsed = parse_unified_diff(
                                raw,
                                context_lines=context_lines,
                                metadata={"files": [entry]},
                                max_bytes=max_bytes,
                            )
                            parsed_files = parsed.get("files") or []
                            if len(parsed_files) != 1:
                                raise ValueError(
                                    "p4 diff2 did not return exactly one file section")
                            parsed_item = dict(parsed_files[0])
                            item.update(parsed_item)
                            item["source"] = "p4-diff2"
                            item["fromRevision"] = left_ref
                            item["toRevision"] = right_ref
                            if not item.get("supported", False):
                                errors.append(item)
                        else:
                            # diff2 emits header-only sections for adds and
                            # deletes.  Read the one existing side so those
                            # files still receive safe one-sided anchors.
                            left_bytes = await self._read_revision_bytes(
                                p4, left_spec, max_bytes) if left_spec else b""
                            right_bytes = await self._read_revision_bytes(
                                p4, right_spec, max_bytes) if right_spec else b""
                            if looks_binary(left_bytes, file_type) or looks_binary(right_bytes, file_type):
                                item.update({"binary": True, "supported": False,
                                             "complete": False,
                                             "reason": "binary content detected; line diff unavailable"})
                                errors.append(item)
                            else:
                                diff = build_hunks(left_bytes, right_bytes, context_lines)
                                item.update(diff)
                                item["binary"] = False
                                item["supported"] = True
                                item["complete"] = True
                                if "utf-8-replace" in {
                                        diff.get("oldEncoding"), diff.get("newEncoding")}:
                                    item.update({
                                        "supported": False, "complete": False,
                                        "reason": "content could not be decoded without replacement",
                                        "encodingWarning": True,
                                    })
                                    errors.append(item)
                    except Exception as exc:
                        item.update({"supported": False, "complete": False,
                                     "reason": str(exc)})
                        errors.append(item)
                    expanded.append(item)

        result = {
            "reviewId": review_id,
            "fromVersion": effective_from,
            "toVersion": effective_to,
            "files": expanded,
            "sourceChange": to_change,
            "versionChange": to_change,
            "pending": target_pending,
            "shelfPresent": shelf_present,
            "contextLines": context_lines,
            "maxFiles": max_files,
            "maxBytes": max_bytes,
            "complete": (
                parsed_complete and not errors and not omitted and not metadata_limited
            ),
            "warnings": warnings,
        }
        if metadata_limited:
            result["limited"] = True
            warnings.append("Swarm file metadata was limited; the diff is incomplete.")
        if omitted:
            omitted_source = omitted_paths or [
                entry.get("depotFile") or entry.get("toFile")
                for entry in entries[max_files:]
            ]
            result["omittedFiles"] = [
                path for path in omitted_source if path
            ]
        if errors:
            result["errors"] = [
                {"depotFile": e.get("depotFile"), "reason": e.get("reason")}
                for e in errors
            ]
        # The raw unified stream is useful while debugging the parser but is
        # not part of the line-addressable API contract.  Returning it would
        # duplicate potentially multi-megabyte shelf content in every MCP
        # response and defeat the max-files/max-bytes safeguards.
        result.pop("raw", None)
        return {"status": "success", "message": result}
        
    async def get_review_activity(
            self, 
            review_id: int, 
            max_results: int = 100
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews/{id}/activity - Get activity for a review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/activity"

            params = {}
            if max_results:
                params["max"] = max_results

            r = requests.get(url, auth=auth, params=params if params else None, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to get activity for review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}
        
    async def get_review_comments(
            self, 
            review_id: int
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews/{id}/comments - Get a list of comments on a review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/comments"
            r = requests.get(url, auth=auth, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to get comments for review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    # ============================================================================
    # POST endpoints
    # ============================================================================

    async def create_review(
            self,
            change_id: int,
            description: Optional[str] = None,
            reviewers: Optional[List[str]] = None,
            required_reviewers: Optional[List[str]] = None,
            reviewer_groups: Optional[List[Dict[str, Any]]] = None,
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews - Create a new review
        
        Args:
            change_id = "12345"
            description = "This is the review description."
            reviewers = ["raj", "mei"]
            required_reviewers = ["vera", "dai"]
            reviewer_groups = [
                {"name":"WebDesigners", "required":"true", "quorum":"1"},
                {"name":"Developers", "required":"true"},
                {"name":"Administrators"}
            ]
            state = "needsReview"
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews"
            payload: Dict[str, Any] = {"change": change_id}

            if description:
                payload["description"] = description

            if reviewers:
                payload["reviewers"] = reviewers

            if required_reviewers:
                payload["requiredReviewers"] = required_reviewers

            if reviewer_groups:
                payload["reviewerGroups"] = reviewer_groups

            r = requests.post(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to create review for changelist '{change_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def refresh_review_projects(
            self, 
            review_id: int
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/refreshProjects - Refresh project associations for a review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/refreshProjects"
            r = requests.post(url, auth=auth, json={}, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to refresh projects for review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def vote_review(
            self,
            review_id: int,
            vote_value: str = "up",
            version: Optional[int] = None
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/vote - Vote on a review
        
        Args:
            review_id = "12345"
            vote_value = "up"|"down"|"clear"
            version = 1
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/vote"
            payload = {"vote": vote_value}

            if version is not None:
                payload["version"] = version

            r = requests.post(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to vote on review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def transition_review_state(
            self,
            review_id: int,
            transition: str,
            jobs: Optional[List[str]] = None,
            fix_status: Optional[str] = None,
            cleanup: Optional[bool] = None,
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/transitions - Change review state
        
        Args:
            review_id = "12345"                 
            transition = "needsRevision"|"needsReview"|"approved"|"committed"|"approved:commit"|"rejected"|"archived"
            jobs = ["job000001", "job000015"]   
            fix_status = "closed"|"open"
            cleanup = True|False
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/transitions"

            payload: Dict[str, Any] = {"transition": transition}

            if jobs:
                payload["jobs"] = jobs

            if fix_status:
                payload["fixStatus"] = fix_status

            if cleanup is not None:
                payload["cleanup"] = cleanup

            r = requests.post(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to transition review state for '{review_id}': {e}")
            return {"status": "error", "message": str(e)}
        
    async def append_participants(
            self,
            review_id: int,
            users: Optional[List[str]] = None,
            groups: Optional[List[str]] = None
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/participants - Append participants to review
        
        Args:            
            review_id = "12345"
            users = ["alice", "bob"]
            groups = ["dev-team", "qa-team"]
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/participants"
            payload = {"participants": {}}

            # Swarm v11 expects dictionaries keyed by user/group name
            # ({"users": {"bjones": []}}); plain arrays are silently ignored.
            # delete_participants below already follows this convention.
            if users:
                payload["participants"]["users"] = (
                    users if isinstance(users, dict) else {u: [] for u in users})

            if groups:
                payload["participants"]["groups"] = (
                    groups if isinstance(groups, dict) else {g: [] for g in groups})

            r = requests.post(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to append participants for review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def add_review_comment(
            self,
            review_id: int, 
            body: str,
            task_state: Optional[str] = None,
            notify: Optional[str] = None,
            context: Optional[CommentContext] = None,
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/comments - Add a comment to a review
        
        Args:
            review_id = "885"
            body = "This is a comment."
            task_state = "open"|"comment"
            notify = "delayed"|"immediate"
            context = {
                    "file": "//depot/path/to/file.txt",    # string, required if commenting on a file
                    "leftLine": 10,                        # integer: left-side diff line number
                    "rightLine": 12,                       # integer: right-side diff line number
                    "content": [                           # array of strings, optional: code context (lines)
                        "line n-4 text",
                        "line n-3 text",
                        "line n-2 text",
                        "line n-1 text",
                        "line n text"
                    ],
                    "version": 2,                           # integer, optional: review version
                    "attribute": "description",             # string, optional: comment on the review description
                    "comment": 99                           # integer, optional: replying to another comment
                }
        """
        # Keep the wire-level contract guarded even when a caller bypasses the
        # Pydantic ``CommentContext`` model (for example, a direct service
        # integration test or a third-party adapter).
        if context:
            has_left = context.leftLine is not None
            has_right = context.rightLine is not None
            if context.content is not None and not (has_left or has_right):
                return {"status": "error", "message":
                        "comment context content requires line anchors"}
            if has_left != has_right:
                return {"status": "error", "message":
                        "Swarm inline comments require both leftLine and rightLine"}
            if (has_left or has_right) and not context.file:
                return {"status": "error", "message":
                        "comment context file is required for line anchors"}

        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/comments"
            params = {}
            if notify:
                params["notify"] = notify

            payload = {"body": body}

            if context:
                payload["context"] = {}
                if context.file:
                    payload["context"]["file"] = context.file
                if context.leftLine is not None:
                    payload["context"]["leftLine"] = context.leftLine
                if context.rightLine is not None:
                    payload["context"]["rightLine"] = context.rightLine
                if context.content is not None:
                    # Content is part of the line anchor contract.  Preserve
                    # the caller's exact lines (including trailing newlines)
                    # instead of silently replacing them with an empty list.
                    payload["context"]["content"] = list(context.content)
                if context.version is not None:
                    payload["context"]["version"] = context.version
                if context.attribute:
                    payload["context"]["attribute"] = context.attribute
                if context.comment is not None:
                    payload["context"]["comment"] = context.comment

            if task_state:
                payload["taskState"] = task_state

            r = requests.post(url, auth=auth, params=params, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to add comment to review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def reply_to_comment(
            self, 
            review_id: int, 
            comment_id: str,
            body: str
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/comments - Reply to a comment
        
        Args:
            review_id = "885"
            comment_id = "1234"
            body = "This is a reply to the comment."
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/comments"
            payload = {"body": body, "context" : {}}
            payload["context"]["comment"] = int(comment_id)
            r = requests.post(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to reply to comment '{comment_id}' in review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def edit_comment(
            self,
            comment_id: int,
            body: Optional[str] = None,
            task_state: Optional[str] = None,
            notify: Optional[str] = None,
        ) -> Dict[str, Any]:
        """POST /api/v11/comments/{id}/edit - Edit a comment body and/or its task state

        Only fields provided are updated. Swarm only allows the comment's author
        to edit it (403 otherwise). task_state accepts
        "comment"|"open"|"addressed"|"verified". Swarm's docs describe the flow
        open -> addressed -> verified, but live testing against Swarm (API v11)
        showed the server does not enforce the ordering; treat it as the
        recommended convention rather than a hard constraint.

        Args:
            comment_id = 1234
            body = "Updated comment text."
            task_state = "addressed"
            notify = "delayed"|"immediate"
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/comments/{comment_id}/edit"

            payload = {}
            if body is not None:
                payload["body"] = body
            if task_state:
                payload["taskState"] = task_state
            if notify:
                payload["notify"] = notify

            r = requests.post(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to edit comment '{comment_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def append_change_to_review(
            self, 
            review_id: int, 
            change_id: int
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/appendchange - Append a changelist to a pre-commit review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/appendchange"
            payload = {"changeId": change_id}
            r = requests.post(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to append change '{change_id}' to review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}
        
    async def replace_review_with_change(
            self, 
            review_id: int, 
            change_id: int
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/replacewithchange - Replace review with a new change"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/replacewithchange"
            payload = {"changeId": change_id}
            r = requests.post(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}

        except Exception as e:
            logger.error(f"Failed to replace review '{review_id}' with change '{change_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def join_review(
            self, 
            review_id: int
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/join - Join a review as a participant"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/join"
            body = {
                        "participants": {
                            "users": {
                               auth.username : []
                            }
                        }
                    }
            r = requests.post(url, auth=auth, json=body, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to join review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def archive_inactive_reviews(
            self, 
            not_updated_since: str,
            max_reviews: int = 0,
            description: str = "Archiving inactive reviews"
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/archiveInactive - Archive inactive reviews
        
        Args:
            not_updated_since = "2023-06-06"
            max_reviews = 50
            description = "This is the description"
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/archiveInactive"
            payload = {
                "notUpdatedSince": not_updated_since,
                "description": description
            }

            if max_reviews > 0:
                payload["max"] = max_reviews

            r = requests.post(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to archive inactive reviews: {e}")
            return {"status": "error", "message": str(e)}
        

    # ============================================================================
    # POST Comments endpoints
    # ============================================================================

    async def mark_comment_as_read(
            self, 
            comment_id: int
        ) -> Dict[str, Any]:
        """POST /api/v11/comments/{id}/read - Mark a comment as read"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/comments/{comment_id}/read"
            r = requests.post(url, auth=auth, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to mark comment '{comment_id}' as read: {e}")
            return {"status": "error", "message": str(e)}
        
    async def mark_comment_as_unread(
            self,
            comment_id: int
        ) -> Dict[str, Any]:
        """POST /api/v11/comments/{id}/unread - Mark a comment as unread"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/comments/{comment_id}/unread"
            r = requests.post(url, auth=auth, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to mark comment '{comment_id}' as unread: {e}")
            return {"status": "error", "message": str(e)}
        
    async def mark_all_comments_as_read(
            self,
            review_id: int
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/comments/read - Mark all comments in a review as read"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/comments/read"
            r = requests.post(url, auth=auth, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to mark all comments in review '{review_id}' as read: {e}")
            return {"status": "error", "message": str(e)}
        
    async def mark_all_comments_as_unread(
            self,
            review_id: int
        ) -> Dict[str, Any]:
        """POST /api/v11/reviews/{id}/comments/unread - Mark all comments in a review as unread"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/comments/unread"
            r = requests.post(url, auth=auth, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to mark all comments in review '{review_id}' as unread: {e}")
            return {"status": "error", "message": str(e)}

    # ============================================================================
    # PUT endpoints
    # ============================================================================

    async def update_review_author(
            self, 
            review_id: int, 
            new_author: str
        ) -> Dict[str, Any]:
        """PUT /api/v11/reviews/{id}/author - Update review author

        Args:
            review_id: The review ID
            new_author: The new author username
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/author"
            payload = {"author": new_author}
            r = requests.put(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to update author for review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def update_review_description(
            self, 
            review_id: int, 
            new_description: str
        ) -> Dict[str, Any]:
        """PUT /api/v11/reviews/{id}/description - Update review description

        Args:
            review_id: The review ID
            new_description: The new description text
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/description"
            payload = {"description": new_description}
            r = requests.put(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to update description for review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def replace_participants(
            self, 
            review_id: int, 
            users: Optional[Dict[str, Dict[str, str]]] = None,
            groups: Optional[Dict[str, Dict[str, str]]] = None
        ) -> Dict[str, Any]:
        """PUT /api/v11/reviews/{id}/participants - Replace all participants
        
        Args:
            review_id: The review ID
            users: Dict of username -> {"required": "yes"|"no"}
            groups: Dict of groupname -> {"required": "none"|"all"|"one"}
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/participants"
            payload = {"participants": {}}
            if users:
                payload["participants"]["users"] = users
            if groups:
                payload["participants"]["groups"] = groups
                        
            r = requests.put(url, auth=auth, json=payload, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to replace participants for review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    # ============================================================================
    # DELETE endpoints
    # ============================================================================

    async def delete_participants(self, review_id: int,
                                 users: Optional[List[str]] = None,
                                 groups: Optional[List[str]] = None) -> Dict[str, Any]:
        """DELETE /api/v11/reviews/{id}/participants - Delete participants from review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/participants"

            body = {"participants": {}}

            if users:
                body["participants"]["users"] = {u: [] for u in users}

            if groups:
                body["participants"]["groups"] = {g: [] for g in groups}
                
            r = requests.delete(url, auth=auth, json=body, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to delete participants from review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def leave_review(self, review_id: int) -> Dict[str, Any]:
        """DELETE /api/v11/reviews/{id}/leave - Leave a review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            body = {
                        "participants": {
                            "users": {
                               auth.username : []
                            }
                        }
                    }
            
            url = f"{api_base}/reviews/{review_id}/leave"
            r = requests.delete(url, auth=auth, json=body, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to leave review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}

    async def obliterate_review(self, review_id: int) -> Dict[str, Any]:
        """DELETE /api/v11/reviews/{id} - Obliterate (permanently delete) a review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}"
            r = requests.delete(url, auth=auth, verify=self.verify_ssl)
            return {"status": "success", "message": self._handle_response(r)}
        except Exception as e:
            logger.error(f"Failed to obliterate review '{review_id}': {e}")
            return {"status": "error", "message": str(e)}
