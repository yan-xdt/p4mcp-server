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
- edit_comment : POST /api/v11/comments/{id}/edit
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
from .structured_diff import (
    DEFAULT_MAX_TOTAL_BYTES,
    InventoryFingerprintMismatch,
    MIN_MAX_TOTAL_BYTES,
    StructuredDiffPage,
    build_structured_file,
    file_filter_identity,
    filter_file_inventory,
    finish_metadata_page,
    normalize_expected_fingerprint,
    normalize_file_filters,
    prepare_diff_page,
    validate_expected_fingerprint,
)

logger = logging.getLogger(__name__)

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
            if value.get("id") is not None:
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


def _review_reference(payload: Any) -> Optional[dict[str, Any]]:
    """Locate the mutable review object inside a successful Swarm payload."""
    value = payload
    for _ in range(6):
        if isinstance(value, dict):
            if value.get("id") is not None:
                return value
            candidates: list[dict[str, Any]] = []
            for key in ("reviews", "review"):
                candidate = value.get(key)
                if isinstance(candidate, list):
                    if len(candidate) != 1 or not isinstance(candidate[0], dict):
                        return None
                    candidates.append(candidate[0])
                elif isinstance(candidate, dict):
                    candidates.append(candidate)
                elif candidate is not None:
                    return None
            if candidates:
                return candidates[0] if len(candidates) == 1 else None
            nested = value.get("data")
            if isinstance(nested, (dict, list)):
                value = nested
                continue
            return None
        if isinstance(value, list):
            if len(value) != 1 or not isinstance(value[0], dict):
                return None
            value = value[0]
            continue
        return None
    return None


def _transition_fields(payload: Any) -> dict[str, Any]:
    """Extract the independent transitions endpoint's fields.

    Swarm uses two JSON shapes for ``blocked``: an empty/list form when there
    are no blocking reasons, and a state-keyed object when it can explain why
    a transition is unavailable (for example, missing required votes). Keep
    either valid shape intact instead of treating the explanatory object as a
    malformed response.
    """
    value = _payload_data(payload)
    if not isinstance(value, Mapping) or "transitions" not in value:
        raise ValueError("Swarm transitions response did not contain transitions")
    transitions = value.get("transitions")
    if not isinstance(transitions, Mapping):
        raise ValueError(
            "Swarm transitions response contained an unsupported transitions shape")
    result = {"transitions": dict(transitions)}
    if "blocked" in value:
        blocked = value.get("blocked")
        if isinstance(blocked, list):
            result["blocked"] = list(blocked)
        elif isinstance(blocked, Mapping):
            result["blocked"] = dict(blocked)
        else:
            raise ValueError(
                "Swarm transitions response contained an unsupported blocked shape")
    return result


def _review_files_payload(payload: Any) -> tuple[list[dict[str, Any]], bool]:
    """Extract review file metadata and the Swarm ``limited`` indicator."""
    # Keep an outer ``limited`` flag as well as the normal data.limited form;
    # reverse proxies have emitted both shapes.
    outer_limited = False
    if isinstance(payload, Mapping) and "limited" in payload:
        outer_limited_value = _as_bool(payload.get("limited"))
        if outer_limited_value is None:
            raise ValueError("Swarm response contained an invalid outer limited flag")
        outer_limited = outer_limited_value
    data = _payload_data(payload)
    limited = False
    if isinstance(data, Mapping):
        if "limited" in data:
            limited_value = _as_bool(data.get("limited"))
            if limited_value is None:
                raise ValueError("Swarm response contained an invalid limited flag")
            limited = limited_value
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
            added_identity_field = False

            if fields:
                requested_fields = list(fields)
                query_fields = list(requested_fields)
                if include_transitions and "id" not in query_fields:
                    query_fields.append("id")
                    added_identity_field = True
                params["fields[]"] = query_fields

            r = requests.get(url, auth=auth, params=params if params else None, verify=self.verify_ssl)
            payload = self._handle_response(r)
            if include_transitions:
                # The query-string flag is ignored by supported Swarm v11
                # servers. Fetch the dedicated endpoint and merge its fields
                # into the same review object callers already consume.
                transitions_response = requests.get(
                    f"{url}/transitions",
                    auth=auth,
                    verify=self.verify_ssl,
                )
                transitions_payload = self._handle_response(transitions_response)
                review = _review_reference(payload)
                if review is None:
                    raise ValueError(
                        "Swarm review response did not contain one review for transitions"
                    )
                returned_id = review.get("id")
                if (returned_id is None
                        or str(returned_id).strip() != str(review_id).strip()):
                    raise ValueError(
                        "Swarm review response omitted or changed the requested review id"
                    )
                review.update(_transition_fields(transitions_payload))
                if added_identity_field:
                    review.pop("id", None)
            return {"status": "success", "message": payload}
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
            to_version: Optional[int] = None,
            max_files: Optional[int] = None,
            after_file: Optional[str] = None,
            max_total_bytes: Optional[int] = None,
            exclude_types: Optional[List[str]] = None,
            exclude_globs: Optional[List[str]] = None,
            expected_inventory_fingerprint: Optional[str] = None,
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews/{id}/files?from={x}&to={y}
        Get files changed between review versions.

        With ``max_files`` (as used by the public handler), the lightweight
        Swarm inventory is normalized, filtered, sorted, cursor-paged, and
        size-bounded locally. Calls without paging/filter arguments retain the
        historical raw Swarm envelope for internal compatibility.
        """
        try:
            normalized_exclude_types, normalized_exclude_globs = (
                normalize_file_filters(exclude_types, exclude_globs)
            )
        except ValueError as exc:
            return self._diff_error(
                review_id, exc, stage="file-filter", retryable=False)
        try:
            expected_fingerprint = normalize_expected_fingerprint(
                expected_inventory_fingerprint)
        except ValueError as exc:
            return self._diff_error(
                review_id, exc, stage="file-pagination", retryable=False)
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
            payload = self._handle_response(r)

            advanced = any((
                max_files is not None,
                after_file is not None,
                max_total_bytes is not None,
                bool(exclude_types),
                bool(exclude_globs),
                expected_inventory_fingerprint is not None,
            ))
            if not advanced:
                return {"status": "success", "message": payload}

            effective_max_files = 200 if max_files is None else max_files
            effective_max_total_bytes = (
                DEFAULT_MAX_TOTAL_BYTES
                if max_total_bytes is None else max_total_bytes
            )
            if effective_max_files < 1:
                return {"status": "error", "message": "max_files must be positive"}
            if effective_max_total_bytes < MIN_MAX_TOTAL_BYTES:
                return {
                    "status": "error",
                    "message": (
                        f"max_total_bytes must be at least {MIN_MAX_TOTAL_BYTES}"
                    ),
                }

            entries, metadata_limited = _review_files_payload(payload)
            selected, filter_summary = filter_file_inventory(
                entries,
                normalized_exclude_types,
                normalized_exclude_globs,
            )
            identity = {
                "kind": "review-files",
                "reviewId": review_id,
                "fromVersion": from_version,
                "toVersion": to_version,
                **file_filter_identity(filter_summary),
            }
            plan = prepare_diff_page(
                selected,
                effective_max_files,
                after_file,
                inventory_identity=identity,
            )
            try:
                validate_expected_fingerprint(
                    plan.inventory_fingerprint,
                    expected_fingerprint,
                )
            except (InventoryFingerprintMismatch, ValueError) as exc:
                mismatch = isinstance(exc, InventoryFingerprintMismatch)
                return self._diff_error(
                    review_id,
                    exc,
                    stage="file-pagination",
                    expectedInventoryFingerprint=expected_fingerprint,
                    inventoryFingerprint=plan.inventory_fingerprint,
                    restartRequired=mismatch,
                    retryable=False,
                )

            base = {
                "reviewId": review_id,
                "fromVersion": from_version,
                "toVersion": to_version,
                "source": "swarm-review-files",
                "fileFilters": filter_summary,
                "warnings": [],
            }
            result = finish_metadata_page(
                plan,
                effective_max_total_bytes,
                base,
                metadata_limited=metadata_limited,
            )
            return {"status": "success", "message": result}
        except ValueError as exc:
            stage = "file-pagination" if "pagination" in str(exc) else "review-files"
            return self._diff_error(
                review_id,
                exc,
                stage=stage,
                restartRequired=stage == "file-pagination",
                retryable=stage != "file-pagination",
            )
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

    @staticmethod
    def _expand_review_file_entries(payload: Any) -> tuple[list[dict[str, Any]], bool]:
        return _review_files_payload(payload)

    @staticmethod
    def _p4_describe_shelf(p4, changelist_id: str) -> list[dict[str, Any]]:
        """Read and normalize the tagged pending-shelf record for a CL."""
        previous_tagged = getattr(p4, "tagged", True)
        try:
            p4.tagged = True
            runner = getattr(p4, "run_describe", None)
            if callable(runner):
                result = runner("-s", "-S", str(changelist_id))
            else:
                result = p4.run("describe", "-s", "-S", str(changelist_id))
            ReviewServices._check_p4_output(
                p4, result, f"p4 describe -s -S {changelist_id}")
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
            after_file: Optional[str] = None,
            max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
            exclude_types: Optional[List[str]] = None,
            exclude_globs: Optional[List[str]] = None,
            expected_inventory_fingerprint: Optional[str] = None,
        ) -> Dict[str, Any]:
        """Return a bounded, line-addressable page for a review version range.

        The latest pending version is read from its authoritative shelf
        inventory. Metadata is sorted and paged before any content command is
        issued; each selected file is then expanded independently. Binary and
        oversized files remain visible as unsupported summaries without
        unsafe or partial hunks.
        """
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
        try:
            # Validate once before any network or P4 work. The normalized
            # values are also used for deterministic filter identity below.
            normalized_exclude_types, normalized_exclude_globs = (
                normalize_file_filters(exclude_types, exclude_globs)
            )
        except ValueError as exc:
            return self._diff_error(
                review_id, exc, stage="file-filter", retryable=False)
        try:
            expected_fingerprint = normalize_expected_fingerprint(
                expected_inventory_fingerprint)
        except ValueError as exc:
            return self._diff_error(
                review_id, exc, stage="file-pagination", retryable=False)
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
                review_id,
                "Swarm response did not contain exactly one review object "
                "with the requested review id",
                stage="review-metadata",
                retryable=False,
            )
        returned_id = review.get("id")
        if (returned_id is None
                or str(returned_id).strip() != str(review_id).strip()):
            return self._diff_error(
                review_id,
                "Swarm metadata omitted or changed the requested review id",
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
        if effective_from is not None and (
                effective_from < 0 or effective_from >= effective_to):
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
        if effective_from and not from_change:
            return self._diff_error(
                review_id, "Starting review version has no usable changelist",
                stage="review-version", version=effective_from, retryable=False)

        version_has_pending = "pending" in to_record
        version_pending_value = (
            _as_bool(to_record.get("pending")) if version_has_pending else None
        )
        if version_has_pending and version_pending_value is None:
            return self._diff_error(
                review_id,
                "Selected review version has an invalid pending flag",
                stage="review-version",
                version=effective_to,
                pendingKnown=False,
                retryable=False,
            )
        review_has_pending = (
            effective_to == total_versions and "pending" in review
        )
        review_pending_value = (
            _as_bool(review.get("pending")) if review_has_pending else None
        )
        if (version_pending_value is not None
                and review_pending_value is not None
                and version_pending_value != review_pending_value):
            return self._diff_error(
                review_id,
                "Review pending metadata is contradictory between the selected "
                "latest version and the review object",
                stage="review-version",
                version=effective_to,
                pendingKnown=False,
                retryable=True,
            )
        target_pending_value = version_pending_value
        # Older Swarm payloads can omit the per-version flag.  Only absence
        # permits the documented latest-review fallback; an explicit null or
        # malformed value above remains an error instead of silently trusting
        # a different field.
        if not version_has_pending and effective_to == total_versions:
            target_pending_value = review_pending_value
        if target_pending_value is None:
            return self._diff_error(
                review_id,
                "Selected review version has no reliable pending flag",
                stage="review-version",
                version=effective_to,
                pendingKnown=False,
                retryable=False,
            )
        target_pending = target_pending_value is True
        if to_version is None and not target_pending:
            return self._diff_error(
                review_id,
                "The latest review version is not pending; there is no live "
                "review shelf to use as the default diff source",
                stage="review-version",
                version=effective_to,
                pending=False,
                shelfPresent=False,
                retryable=False,
            )

        metadata_limited = False
        shelf_present = False
        source = "p4-file-diff"
        try:
            if target_pending and effective_from is None:
                # The current pending shelf is authoritative. describe -s -S
                # returns only its lightweight file inventory; unlike -du it
                # never transfers the complete shelf before max_files applies.
                async with self.connection_manager.get_connection() as p4:
                    entries = self._p4_describe_shelf(p4, to_change)
                    selected_entries, filter_summary = filter_file_inventory(
                        entries,
                        normalized_exclude_types,
                        normalized_exclude_globs,
                    )
                    plan = prepare_diff_page(
                        selected_entries,
                        max_files,
                        after_file,
                        inventory_identity={
                            "kind": "review-shelf",
                            "reviewId": review_id,
                            "fromVersion": effective_from,
                            "toVersion": effective_to,
                            "fromChange": from_change,
                            "toChange": to_change,
                            "pending": target_pending,
                            **file_filter_identity(filter_summary),
                        },
                    )
                    try:
                        validate_expected_fingerprint(
                            plan.inventory_fingerprint,
                            expected_fingerprint,
                        )
                    except (InventoryFingerprintMismatch, ValueError) as exc:
                        mismatch = isinstance(exc, InventoryFingerprintMismatch)
                        return self._diff_error(
                            review_id,
                            exc,
                            stage="file-pagination",
                            sourceChange=to_change,
                            fromVersion=effective_from,
                            toVersion=effective_to,
                            expectedInventoryFingerprint=(
                                expected_fingerprint),
                            inventoryFingerprint=plan.inventory_fingerprint,
                            restartRequired=mismatch,
                            retryable=False,
                        )
                    page = StructuredDiffPage(plan, max_total_bytes)
                    for entry in plan.candidates:
                        item = await build_structured_file(
                            p4,
                            entry,
                            target_pending=True,
                            to_change=to_change,
                            from_change=None,
                            effective_from=None,
                            context_lines=context_lines,
                            max_bytes=max_bytes,
                            check_output=self._check_p4_output,
                        )
                        if not page.append(item) or page.budget_limited:
                            break
                shelf_present = True
                source = "p4-shelf-files"
            else:
                files_result = await self.get_review_files(
                    review_id,
                    from_version=effective_from,
                    to_version=effective_to,
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
                    return self._diff_error(
                        review_id, exc, stage="review-files", retryable=True)
                if not entries:
                    return self._diff_error(
                        review_id,
                        "Swarm returned an empty review file list; refusing to claim a complete diff",
                        stage="review-files",
                        fromVersion=effective_from,
                        toVersion=effective_to,
                        complete=False,
                        retryable=True,
                    )
                selected_entries, filter_summary = filter_file_inventory(
                    entries,
                    normalized_exclude_types,
                    normalized_exclude_globs,
                )
                plan = prepare_diff_page(
                    selected_entries,
                    max_files,
                    after_file,
                    inventory_identity={
                        "kind": "review-range",
                        "reviewId": review_id,
                        "fromVersion": effective_from,
                        "toVersion": effective_to,
                        "fromChange": from_change,
                        "toChange": to_change,
                        "pending": target_pending,
                        **file_filter_identity(filter_summary),
                    },
                )
                try:
                    validate_expected_fingerprint(
                        plan.inventory_fingerprint,
                        expected_fingerprint,
                    )
                except (InventoryFingerprintMismatch, ValueError) as exc:
                    mismatch = isinstance(exc, InventoryFingerprintMismatch)
                    return self._diff_error(
                        review_id,
                        exc,
                        stage="file-pagination",
                        sourceChange=to_change,
                        fromVersion=effective_from,
                        toVersion=effective_to,
                        expectedInventoryFingerprint=expected_fingerprint,
                        inventoryFingerprint=plan.inventory_fingerprint,
                        restartRequired=mismatch,
                        retryable=False,
                    )
                page = StructuredDiffPage(plan, max_total_bytes)
                async with self.connection_manager.get_connection() as p4:
                    if target_pending:
                        self._p4_describe_shelf(p4, to_change)
                        shelf_present = True
                    for entry in plan.candidates:
                        item = await build_structured_file(
                            p4,
                            entry,
                            target_pending=target_pending,
                            to_change=to_change,
                            from_change=from_change,
                            effective_from=effective_from,
                            context_lines=context_lines,
                            max_bytes=max_bytes,
                            check_output=self._check_p4_output,
                        )
                        if not page.append(item) or page.budget_limited:
                            break
        except ValueError as exc:
            stage = "file-pagination" if "after_file" in str(exc) else "p4-file-diff"
            return self._diff_error(
                review_id,
                exc,
                stage=stage,
                sourceChange=to_change,
                fromVersion=effective_from,
                toVersion=effective_to,
                restartRequired=stage == "file-pagination",
                retryable=stage != "file-pagination",
            )
        except Exception as exc:
            return self._diff_error(
                review_id,
                f"Could not read structured review diff: {exc}",
                stage="p4-file-diff",
                sourceChange=to_change,
                fromVersion=effective_from,
                toVersion=effective_to,
                retryable=True,
            )

        base = {
            "reviewId": review_id,
            "fromVersion": effective_from,
            "toVersion": effective_to,
            "sourceChange": to_change,
            "versionChange": to_change,
            "pending": target_pending,
            "shelfPresent": shelf_present,
            "contextLines": context_lines,
            "maxFiles": max_files,
            "maxBytes": max_bytes,
            "source": source,
            "fileFilters": filter_summary,
            "warnings": [],
        }
        try:
            result = page.finish(base, metadata_limited=metadata_limited)
        except ValueError as exc:
            return self._diff_error(
                review_id,
                exc,
                stage="response-budget",
                sourceChange=to_change,
                fromVersion=effective_from,
                toVersion=effective_to,
                retryable=False,
            )
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
            review_id: int,
            fields: Optional[str] = None,
        ) -> Dict[str, Any]:
        """GET /api/v11/reviews/{id}/comments - Get a list of comments on a review"""
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/reviews/{review_id}/comments"
            params = {"fields": fields} if fields else None
            r = requests.get(url, auth=auth, params=params, verify=self.verify_ssl)
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
        """POST /api/v11/comments/{id}/edit - Edit an existing comment

        Comment-scoped: targets a comment by comment_id alone; review_id is
        neither required nor sent. Only the fields the caller supplies are
        forwarded so omitted fields are left untouched on the server. Swarm
        only allows the comment's author to edit it (403 otherwise).
        task_state accepts "comment"|"open"|"addressed"|"verified". Swarm's
        docs describe the flow open -> addressed -> verified, but live testing
        against Swarm (API v11) showed the server does not enforce the
        ordering; treat it as the recommended convention rather than a hard
        constraint.

        Args:
            comment_id = "1234"
            body = "Updated comment text."   # only sent when non-empty (truthy)
            task_state = "open"|"comment"|"addressed"|"verified"
            notify = "delayed"|"immediate"   # forwarded as a query parameter
        """
        try:
            auth = await self._get_auth()
            api_base = await self._get_api_base()
            url = f"{api_base}/comments/{comment_id}/edit"

            params = {}
            if notify:
                params["notify"] = notify

            payload: Dict[str, Any] = {}
            # A task-state-only edit must never blank the body, so include body
            # only when it is non-empty (truthy) — matching the model validator.
            if body:
                payload["body"] = body
            if task_state:
                payload["taskState"] = task_state

            r = requests.post(url, auth=auth, params=params, json=payload, verify=self.verify_ssl)
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
