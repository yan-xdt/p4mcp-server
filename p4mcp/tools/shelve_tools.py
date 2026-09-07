"""Shelve query & modify tools."""

from __future__ import annotations

from typing import Annotated, Optional, List, Literal, TYPE_CHECKING

from pydantic import Field
from fastmcp import Context

from ..models import shelve_models as m
from .common import handle_with_logging, handle_modify_with_delete_gate

if TYPE_CHECKING:
    from ..server import P4MCPServer


def register(server: "P4MCPServer") -> None:
    if "shelves" not in server.toolsets:
        return

    # ── read ────────────────────────────────────────────────────────────
    @server.mcp.tool(tags=["read", "shelves"])
    async def query_shelves(
        action: Annotated[Literal["list", "diff", "files"], Field(
            description="Shelve query action: list returns all shelved changelists, diff shows shelved file differences, files lists files in shelved changelist"
        )],
        ctx: Context,
        changelist_id: Annotated[Optional[str], Field(
            default=None,
            description="Changelist ID - required for diff and files actions",
            examples=["12345"],
        )] = None,
        user: Annotated[Optional[str], Field(
            default=None,
            description="Filter by user - for list action",
            examples=["alice"],
        )] = None,
        structured: Annotated[bool, Field(
            default=False,
            description=(
                "For diff action, return line-addressable unified hunks; "
                "false keeps the legacy raw list[str] response"
            ),
        )] = False,
        context_lines: Annotated[int, Field(
            default=3, ge=0, le=100,
            description="Unchanged context lines requested for structured diff",
        )] = 3,
        max_files: Annotated[int, Field(
            default=200, ge=1, le=1000,
            description="Maximum files in one structured-diff page; applied before content is read",
        )] = 200,
        max_bytes: Annotated[int, Field(
            default=5_000_000, ge=1, le=100_000_000,
            description="Maximum parsed bytes per file in a structured shelf diff",
        )] = 5_000_000,
        after_file: Annotated[Optional[str], Field(
            default=None,
            description=(
                "Exclusive structured-diff cursor: exact depot path from the "
                "preceding page's lastSeen field"
            ),
        )] = None,
        max_total_bytes: Annotated[int, Field(
            default=10_000_000, ge=65_536, le=100_000_000,
            description="Hard UTF-8 JSON byte budget for one structured-diff payload",
        )] = 10_000_000,
        max_results: Annotated[int, Field(
            default=100, ge=1, le=1000,
            description="Maximum number of results to return",
        )] = 100,
    ) -> dict:
        """List shelves, get shelve files, or a raw/structured diff (READ permission).

        ``diff`` returns the legacy raw list when ``structured`` is false;
        structured mode exposes conservative hunk and line-anchor records.
        """
        params = m.QueryShelvesParams(
            action=action, changelist_id=changelist_id,
            user=user, structured=structured, context_lines=context_lines,
            max_files=max_files, max_bytes=max_bytes,
            after_file=after_file, max_total_bytes=max_total_bytes,
            max_results=max_results,
        )
        return await handle_with_logging(server, "query", "shelves", params, "query_shelves", ctx)

    # ── write ───────────────────────────────────────────────────────────
    if server.readonly:
        return

    @server.mcp.tool(tags=["write", "shelves"])
    async def modify_shelves(
        action: Annotated[Literal["shelve", "unshelve", "update", "delete", "unshelve_to_changelist"], Field(
            description="Shelve modification action: shelve stores files to shelf, unshelve restores files from shelf, update modifies shelved files, delete removes shelf, unshelve_to_changelist restores to specific changelist"
        )],
        changelist_id: Annotated[str, Field(
            description="Changelist ID",
            examples=["12345"],
        )],
        ctx: Context,
        file_paths: Annotated[Optional[List[str]], Field(
            default=None,
            description="File paths for shelve/unshelve/update/delete",
            examples=[["//depot/projectX/file1.txt"]],
        )] = None,
        target_changelist: Annotated[str, Field(
            default="default",
            description="Target changelist for unshelve operations",
            examples=["default", "54321"],
        )] = "default",
        force: Annotated[bool, Field(
            default=False,
            description="Force operation - use with caution",
        )] = False,
    ) -> dict:
        """Create/delete, update shelves and unshelve files (WRITE permission)"""
        params = m.ModifyShelvesParams(
            action=action, changelist_id=changelist_id,
            file_paths=file_paths, target_changelist=target_changelist,
            force=force,
        )
        return await handle_modify_with_delete_gate(
            server, "shelves", params, "modify_shelves", ctx,
            f"Requires approval to delete shelve for changelist: {changelist_id}",
        )
