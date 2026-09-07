"""Shelve query and modify models."""

from typing import Optional, List
from enum import Enum
from pydantic import Field, model_validator
from .common import BaseParams, PaginatedParams


class ShelveAction(str, Enum):
    LIST = "list"
    DIFF = "diff"
    FILES = "files"


class ShelveModifyAction(str, Enum):
    SHELVE = "shelve"
    UNSHELVE = "unshelve"
    UPDATE = "update"
    DELETE = "delete"
    UNSHELVE_TO_CHANGELIST = "unshelve_to_changelist"


class QueryShelvesParams(PaginatedParams):
    """Shelve query parameters."""
    action: ShelveAction = Field(
        description="Shelve query action",
        examples=["list", "diff", "files"]
    )
    changelist_id: Optional[str] = Field(
        default=None,
        description="Changelist ID - required for diff and files actions",
        examples=["12345"]
    )
    user: Optional[str] = Field(
        default=None,
        description="Filter by user - for list action",
        examples=["alice"]
    )
    structured: bool = Field(
        default=False,
        description=(
            "Return unified hunks with left/right line anchors for diff action; "
            "false preserves the legacy raw list[str] response"
        ),
    )
    context_lines: int = Field(
        default=3,
        ge=0,
        le=100,
        description="Requested unchanged context lines for a structured diff",
    )
    max_files: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="Maximum files in one structured-diff page; applied before content is read",
    )
    max_bytes: int = Field(
        default=5_000_000,
        ge=1,
        le=100_000_000,
        description="Maximum parsed bytes per file in a structured shelf diff",
    )
    after_file: Optional[str] = Field(
        default=None,
        description=(
            "Exclusive structured-diff file cursor. Pass the exact lastSeen "
            "depot path from the preceding page."
        ),
    )
    max_total_bytes: int = Field(
        default=10_000_000,
        ge=65_536,
        le=100_000_000,
        description="Hard UTF-8 JSON byte budget for one structured-diff payload",
    )

    @model_validator(mode='after')
    def validate_changelist_id_for_actions(self):
        """Validate changelist_id for specific actions."""
        if self.action in [ShelveAction.DIFF, ShelveAction.FILES] and not self.changelist_id:
            raise ValueError(f'changelist_id is required for action: {self.action}')
        return self


class ModifyShelvesParams(BaseParams):
    """Shelve modification parameters."""
    action: ShelveModifyAction = Field(
        description="Shelve modification action",
        examples=["shelve", "unshelve", "update", "delete", "unshelve_to_changelist"]
    )
    changelist_id: str = Field(
        description="Changelist ID",
        examples=["12345"]
    )
    file_paths: Optional[List[str]] = Field(
        default=None,
        description="File paths required for shelve/unshelve/update/delete - unused for others",
        examples=[["//depot/projectX/file1.txt"]]
    )
    target_changelist: str = Field(
        default="default",
        description="Target changelist for unshelve operations",
        examples=["default", "54321"]
    )
    force: bool = Field(
        default=False,
        description="Force operation - use with caution",
        examples=[False, True]
    )

    @model_validator(mode='after')
    def validate_shelve_params(self):
        """Validate parameters based on action type."""
        if not self.changelist_id:
            raise ValueError('changelist_id is required for all shelve actions')
        actions_requiring_file_paths = [
            ShelveModifyAction.SHELVE,
            ShelveModifyAction.UNSHELVE,
            ShelveModifyAction.UPDATE,
        ]
        if self.action in actions_requiring_file_paths and not self.file_paths:
            raise ValueError(f'file_paths is required for action: {self.action}')
        return self
