from typing import Annotated, Any, Dict, List, Optional
from .common import BaseParams, PaginatedParams
from pydantic import Field, StringConstraints, model_validator, field_validator
from enum import Enum
import re

class ReviewTransition(str, Enum):
    NEEDS_REVISION = "needsRevision"
    NEEDS_REVIEW = "needsReview"
    APPROVED = "approved"
    COMMITTED = "committed"
    APPROVED_COMMIT = "approved:commit"
    REJECTED = "rejected"
    ARCHIVED = "archived"

class VoteValue(str, Enum):
    UP = "up"
    DOWN = "down"
    CLEAR = "clear"

class ReviewAction(str, Enum):
    LIST = "list"
    DASHBOARD = "dashboard"
    GET = "get"
    TRANSITIONS = "transitions"
    FILES_READBY = "files_readby"
    FILES = "files"
    ACTIVITY = "activity"
    COMMENTS = "comments"
    DIFF = "diff"

class QueryReviewsParams(PaginatedParams):
    """Review query parameters."""

    action: ReviewAction = Field(
        description=(
            "Review query action: list all reviews, dashboard, get, transitions, files, "
            "files_readby, comments, activity, or diff. "
            "'dashboard' for current user (my reviews, needs my attention, authenticated user reviews), "
            "use structured=true with files (diff is always line-addressable) for hunks"
        ),
        examples=["list", "dashboard", "get", "transitions", "files_readby", "files", "diff", "comments", "activity"],
    )
    review_id: Optional[int] = Field(
        default=None,
        description="Review ID—required for get, transitions, files_readby, files, comments, activity, and diff actions",
        examples=[12345, 67890],
    )
    fields: Optional[List[str]] = Field(
        default=None,
        description="List of fields to return for list/get actions",
        examples=[["id", "description", "author", "state"], ["id", "author", "state", "participants", "commits"]],
    )
    comments_fields: Optional[str] = Field(
        default="id,body,user,time",
        description="Comma-separated list of fields to return for comments action",
        examples=["id,body,user,time", "id,user,time"],
    )
    up_voters: Optional[List[str]] = Field(
        default=None,
        description="List of up voters for transitions action",
        examples=[["alice", "bob"]],
    )
    from_version: Optional[int] = Field(
        default=None,
        description="Starting version for files/diff action (0 means the depot base)",
        examples=[1, 2],
    )
    to_version: Optional[int] = Field(
        default=None,
        description="Ending version for files/diff action",
        examples=[2, 3],
    )
    structured: bool = Field(
        default=False,
        description=(
            "For files, return line-addressable hunks when true; false preserves "
            "the metadata-only response. The diff action is always structured."
        ),
    )
    context_lines: int = Field(
        default=3,
        ge=0,
        le=100,
        description="Number of unchanged context lines in structured diff hunks",
        examples=[3, 0],
    )
    max_files: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="Maximum number of files to expand in a structured diff",
        examples=[50, 200],
    )
    max_bytes: int = Field(
        default=5_000_000,
        ge=1,
        le=100_000_000,
        description="Maximum bytes to read per file in a structured diff",
        examples=[1000000, 5000000],
    )
    max_results: Optional[int] = Field(
        default=10,
        description="Maximum number of results to return",
        examples=[10, 20, 50],
    )
    # v11 list filters
    after: Optional[str] = Field(
        default=None,
        description="Review ID to seek to for pagination (list action). Reviews up to and including this ID are excluded.",
        examples=["12344"],
    )
    after_updated: Optional[str] = Field(
        default=None,
        description="Return reviews updated on the day before this date/time in seconds since epoch (list action). Mutually exclusive with 'after'.",
        examples=["1606233362"],
    )
    result_order: Optional[str] = Field(
        default=None,
        description="Set to 'updated' to return most recently updated reviews first (list action)",
        examples=["updated"],
    )
    projects: Optional[List[str]] = Field(
        default=None,
        description="Filter by project name(s) (list action)",
        examples=[["myproject"], ["myproject", "gemini"]],
    )
    state: Optional[List[str]] = Field(
        default=None,
        description="Filter by review state(s) (list action). Valid: needsRevision, needsReview, approved, approved:isPending, approved:commit, approved:notPending, rejected, archived",
        examples=[["needsReview"], ["needsReview", "needsRevision", "approved:isPending"]],
    )
    keywords: Optional[str] = Field(
        default=None,
        description="Search keyword(s) to filter reviews (list action). Use with keywords_fields.",
        examples=["bugfix", "12345"],
    )
    keywords_fields: Optional[List[str]] = Field(
        default=None,
        description="Fields to search keywords in (list action). Valid: changes, author, participants, hasReviewer, description, updated, projects, state, testStatus, pending, groups, id",
        examples=[["description"], ["author"], ["changes"]],
    )
    include_transitions: Optional[bool] = Field(
        default=None,
        description="Include allowed state transitions in get action response",
        examples=[True],
    )

    @model_validator(mode="after")
    def validate_review_id_required(self):
        """Ensure review_id is provided for specific actions requiring it."""
        required_actions = {
            ReviewAction.GET,
            ReviewAction.TRANSITIONS,
            ReviewAction.FILES_READBY,
            ReviewAction.FILES,
            ReviewAction.COMMENTS,
            ReviewAction.DIFF,
        }

        if self.action in required_actions and not self.review_id:
            # BaseParams serializes enums to their values, so ``self.action``
            # can be either a ReviewAction instance or a plain string here.
            # Avoid masking the useful validation error with AttributeError.
            action_name = getattr(self.action, "value", self.action)
            raise ValueError(f"review_id is required for action: {action_name}")

        return self

class ReviewModifyAction(str, Enum):
    CREATE = "create"
    REFRESH_PROJECTS = "refresh_projects"
    VOTE = "vote"
    TRANSITION = "transition"
    APPEND_PARTICIPANTS = "append_participants"
    ADD_COMMENT = "add_comment"
    REPLY_COMMENT = "reply_comment"
    EDIT_COMMENT = "edit_comment"
    APPEND_CHANGE = "append_change"
    REPLACE_WITH_CHANGE = "replace_with_change"
    JOIN = "join"
    ARCHIVE_INACTIVE = "archive_inactive"
    MARK_COMMENT_READ = "mark_comment_read"
    MARK_COMMENT_UNREAD = "mark_comment_unread"
    MARK_ALL_COMMENTS_READ = "mark_all_comments_read"
    MARK_ALL_COMMENTS_UNREAD = "mark_all_comments_unread"
    UPDATE_AUTHOR = "update_author"
    UPDATE_DESCRIPTION = "update_description"
    REPLACE_PARTICIPANTS = "replace_participants"
    DELETE_PARTICIPANTS = "delete_participants"
    LEAVE = "leave"
    OBLITERATE = "obliterate"

class FixStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"

class TaskState(str, Enum):
    OPEN = "open"
    COMMENT = "comment"
    ADDRESSED = "addressed"
    VERIFIED = "verified"

class NotifyMode(str, Enum):
    IMMEDIATE = "immediate"
    DELAYED = "delayed"

class CommentContext(BaseParams):
    """Context payload for creating or replying to review comments."""

    # Trailing newlines in ``content`` are part of the Swarm context payload;
    # do not inherit BaseParams' generic whitespace stripping for this model.
    model_config = {
        "str_strip_whitespace": False,
        "validate_assignment": True,
        "extra": "forbid",
        "use_enum_values": True,
    }

    file: Optional[str] = Field(
        default=None,
        description="file mandatory unless attribute or comment are set: File to comment on. " \
        "Valid only for changes and reviews topics",
        examples=["//depot/path/to/file.txt"]
    )
    leftLine: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional left-side diff line number. Deletion comments may use only leftLine. " \
        "Valid only for changes and reviews topics."
    )
    rightLine: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional right-side diff line number. Addition comments may use only rightLine. " \
        "Valid only for changes and reviews topics."
    )
    content: Optional[List[Annotated[str, StringConstraints(strip_whitespace=False)]]] = Field(
        default=None,
        description="Optional array of exact Swarm context lines. Preserve trailing newlines; " \
        "an inline context needs at least one leftLine or rightLine.",
        examples=[["line1\n", "line2\n", "line3\n", "line4\n", "line5\n"]]
    )
    version: Optional[int] = Field(
        default=None,
        ge=1,
        description="integer: With a reviews topic, this field specifies which version to attach the comment to."
    )
    attribute: Optional[str] = Field(
        default=None,
        description="Set to description to comment on the review description"
    )
    comment: Optional[int] = Field(
        default=None,
        ge=1,
        description="integer: Set to the comment id this comment is replying to."
    )

    @field_validator("file")
    @classmethod
    def validate_depot_file(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.startswith("//"):
            raise ValueError("file must be a depot path starting with //")
        # basic depot path sanity check
        if not re.match(r"^//[\w./-]+$", v):
            raise ValueError("Invalid depot file path format")
        return v

    @model_validator(mode="after")
    def validate_context_semantics(self):
        """Validate context semantics."""
        # Structured diff records may use one-sided semantic anchors for
        # additions/deletions, but Swarm's v11 comment endpoint requires both
        # line numbers when a comment is inline.  Keep that transport contract
        # strict here; the diff DTO is intentionally a separate, nullable
        # representation and must not be passed to this model verbatim.
        has_left = self.leftLine is not None
        has_right = self.rightLine is not None
        has_lines = has_left or has_right
        if self.content is not None and not has_lines:
            raise ValueError("content requires leftLine or rightLine")
        if has_lines and not self.file:
            raise ValueError("file is required for a line context")
        if has_lines and not (has_left and has_right):
            raise ValueError(
                "leftLine and rightLine must both be provided for a Swarm inline context")
        if self.content is not None and not all(isinstance(line, str) for line in self.content):
            raise ValueError("content must contain strings")
        return self

class ModifyReviewsParams(BaseParams):
    """Review modification parameters."""
    action: ReviewModifyAction = Field(
        description="Review modification action",
        examples=["create", "vote", "transition", "append_participants"]
    )

    # Common identifiers
    review_id: Optional[int] = Field(
        default=None,
        description="Review ID (required for most actions except create, archive_inactive)",
        examples=[12345]
    )
    change_id: Optional[int] = Field(
        default=None,
        description="Changelist ID (required for create, append_change, replace_with_change)",
        examples=[67890]
    )

    # Create
    description: Optional[str] = Field(
        default=None,
        description="Review description (optional on create)",
        examples=["Implement feature X"]
    )
    reviewers: Optional[List[str]] = Field(
        default=None,
        description="List of reviewers (create_participants)",
        examples=[["alice", "bob"]]
    )
    required_reviewers: Optional[List[str]] = Field(
        default=None,
        description="List of required reviewers (create_participants)",
        examples=[["carol"]]
    )
    reviewer_groups: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Reviewer groups (create_participants)",
        examples=[[{"name": "Developers", "required": "true"}]]
    )
    # Flat reviewer group fields
    reviewer_group_names: Optional[List[str]] = Field(
        default=None,
        description="List of reviewer group names",
        examples=[["Developers", "QA"]]
    )
    reviewer_groups_required: Optional[List[str]] = Field(
        default=None,
        description="List of required reviewer groups",
        examples=[["Architects"]]
    )

    context: Optional[CommentContext] = Field(
        default=None,
        description="Comment context",
        examples=[{"file": "//depot/path/file.txt",
                   "rightLine": 42,
                   "leftLine": 40,
                   "content": ["def example_function():\n", "    pass\n"],
                   "version": 1,
                   "attribute": "description",
                   "comment": 22}]
    )
    # Flat comment context fields
    comment_file_path: Optional[str] = Field(
        default=None,
        description="File path for inline comment",
        examples=["//depot/file.txt"]
    )
    comment_left_line: Optional[int] = Field(
        default=None,
        ge=1,
        description="Left diff line number for inline comment"
    )
    comment_right_line: Optional[int] = Field(
        default=None,
        ge=1,
        description="Right diff line number for inline comment"
    )
    comment_version: Optional[int] = Field(
        default=None,
        ge=1,
        description="Review version for comment attachment"
    )
    # BaseParams enables generic string stripping for ordinary fields.  These
    # strings are different: Swarm uses the exact source context (including
    # indentation and trailing newlines) to validate an inline anchor.
    comment_content: Optional[List[Annotated[str, StringConstraints(strip_whitespace=False)]]] = Field(
        default=None,
        description="Code context lines for an inline comment; trailing newlines are preserved",
        examples=[["line 1\n", "line 2\n"]]
    )

    # Vote
    vote_value: Optional[VoteValue] = Field(
        default=None,
        description="Vote value (vote action)",
        examples=["up", "down", "clear"]
    )
    version: Optional[int] = Field(
        default=None,
        ge=1,
        description="Review version (optional for vote)",
        examples=[2]
    )

    # Transition
    transition: Optional[ReviewTransition] = Field(
        default=None,
        description="Transition target state",
        examples=["approved"]
    )
    jobs: Optional[List[str]] = Field(
        default=None,
        description="Associated job IDs for transition",
        examples=[["job000123", "job000456"]]
    )
    fix_status: Optional[FixStatus] = Field(
        default=None,
        description="Job fix status when transitioning",
        examples=["closed"]
    )
    cleanup: Optional[bool] = Field(
        default=None,
        description="Perform cleanup for approved:commit/committed transitions",
        examples=[True]
    )

    # Participants (structured form)
    users: Optional[Dict[str, Dict[str, str]]] = Field(
        default=None,
        description="Usernames for append/replace/delete participants (username -> {'required': 'yes'|'no'})",
        examples=[{"alice": {"required": "yes"}, "bob": {"required": "no"}}]
    )
    groups: Optional[Dict[str, Dict[str, str]]] = Field(
        default=None,
        description="Group names for append/replace/delete participants (group -> {'required': 'none'|'one'|'all'})",
        examples=[{"dev-team": {"required": "all"}}]
    )
    # Flat participant fields
    participant_user_names: Optional[List[str]] = Field(
        default=None,
        description="List of participant usernames",
        examples=[["alice", "bob"]]
    )
    participant_users_required: Optional[List[str]] = Field(
        default=None,
        description="List of required participant usernames",
        examples=[["carol"]]
    )
    participant_group_names: Optional[List[str]] = Field(
        default=None,
        description="List of participant group names",
        examples=[["dev-team"]]
    )
    participant_groups_required: Optional[List[str]] = Field(
        default=None,
        description="List of required participant groups",
        examples=[["security-team"]]
    )

    # Comments
    body: Optional[str] = Field(
        default=None,
        description="Comment body (required for add_comment, reply_comment)",
        examples=["Looks good."]
    )
    task_state: Optional[TaskState] = Field(
        default=None,
        description="Task state (optional for add_comment: open|comment; "
        "edit_comment additionally accepts addressed|verified)",
        examples=["open"]
    )
    notify: Optional[NotifyMode] = Field(
        default=None,
        description="Notification mode (optional for add_comment)",
        examples=["delayed"]
    )

    comment_id: Optional[int] = Field(
        default=None,
        description="Parent comment ID (reply_comment)",
        examples=[987]
    )

    # Archive inactive
    not_updated_since: Optional[str] = Field(
        default=None,
        description="ISO date (YYYY-MM-DD) threshold for archive_inactive",
        examples=["2024-01-15"]
    )
    max_reviews: Optional[int] = Field(
        default=0,
        ge=0,
        description="Maximum number of inactive reviews to archive (0 = no limit)",
        examples=[50]
    )

    # Update author/description
    new_author: Optional[str] = Field(
        default=None,
        description="New author username (update_author)",
        examples=["dave"]
    )
    new_description: Optional[str] = Field(
        default=None,
        description="New review description (update_description)",
        examples=["Refined implementation details."]
    )

    @model_validator(mode="after")
    def validate_required_fields(self):
        a = self.action

        def need(field: Any, label: Optional[str] = None):
            if not getattr(self, field, None):
                raise ValueError(f"{label or field} is required for action: {a}")

        # Actions requiring review_id
        if a not in [ReviewModifyAction.CREATE, ReviewModifyAction.ARCHIVE_INACTIVE, ReviewModifyAction.EDIT_COMMENT] and a != ReviewModifyAction.CREATE:
            if a not in [ReviewModifyAction.ARCHIVE_INACTIVE] and not self.review_id:
                raise ValueError(f"review_id is required for action: {a}")

        if a == ReviewModifyAction.CREATE:
            need("change_id", "change_id")

        elif a in [ReviewModifyAction.APPEND_CHANGE, ReviewModifyAction.REPLACE_WITH_CHANGE]:
            need("review_id")
            need("change_id", "change_id")

        elif a == ReviewModifyAction.VOTE:
            need("review_id")
            need("vote_value", "vote_value")

        elif a == ReviewModifyAction.TRANSITION:
            need("review_id")
            need("transition", "transition")

        elif a == ReviewModifyAction.ADD_COMMENT:
            need("review_id")
            need("body", "body")
            if self.task_state and self.task_state not in [TaskState.OPEN, TaskState.COMMENT]:
                raise ValueError(
                    "task_state must be 'open' or 'comment' for add_comment; "
                    "'addressed'/'verified' are only reachable via edit_comment"
                )

        elif a == ReviewModifyAction.EDIT_COMMENT:
            need("comment_id", "comment_id")
            if not self.body and not self.task_state:
                raise ValueError("At least one of body or task_state is required for edit_comment action")

        elif a == ReviewModifyAction.REPLY_COMMENT:
            need("review_id")
            need("comment_id", "comment_id")
            need("body", "body")

        elif a == ReviewModifyAction.ARCHIVE_INACTIVE:
            need("not_updated_since", "not_updated_since")

        elif a == ReviewModifyAction.UPDATE_AUTHOR:
            need("review_id")
            need("new_author", "new_author")

        elif a == ReviewModifyAction.UPDATE_DESCRIPTION:
            need("review_id")
            need("new_description", "new_description")

        elif a == ReviewModifyAction.MARK_COMMENT_READ:
            need("comment_id", "comment_id")

        elif a == ReviewModifyAction.MARK_COMMENT_UNREAD:
            need("comment_id", "comment_id")

        elif a == ReviewModifyAction.MARK_ALL_COMMENTS_READ:
            need("review_id")

        elif a == ReviewModifyAction.MARK_ALL_COMMENTS_UNREAD:
            need("review_id")

        elif a == ReviewModifyAction.DELETE_PARTICIPANTS:
            need("review_id")
            if not self.users and not self.groups:
                raise ValueError("At least one of users or groups required for delete_participants action")

        return self
