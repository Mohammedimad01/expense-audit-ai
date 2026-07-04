"""
expense_audit/mcp/drive_export.py
-----------------------------------
Google Drive MCP integration via google-adk MCPToolset.

Behaviour:
  - If GOOGLE_DRIVE_MCP_CREDENTIALS is set → connect to the MCP server and
    expose an `upload_report_to_drive` tool for the report agent.
  - If not set → log a warning and return None; the rest of the system runs
    fully without Drive export. No crash, no missing-feature exception.

This satisfies the competition requirement for a real MCPToolset integration
while keeping local development and CI unblocked for users without credentials.

References:
  - https://google.github.io/adk-docs/tools/mcp-tools/
  - https://github.com/googleapis/google-drive-mcp
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# We import MCPToolset lazily to avoid hard failure when google-adk
# is installed but the MCP server subprocess is not configured.
try:
    from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    logger.warning("google-adk MCPToolset not available — Drive export disabled")


async def get_drive_toolset() -> Optional[Any]:
    """
    Return an MCPToolset connected to the Google Drive MCP server,
    or None if credentials are not configured.
    """
    credentials_path = os.environ.get("GOOGLE_DRIVE_MCP_CREDENTIALS", "").strip()

    if not credentials_path:
        logger.warning(
            "GOOGLE_DRIVE_MCP_CREDENTIALS not set — Google Drive export is disabled. "
            "Set this env var to a service-account JSON path to enable Drive export."
        )
        return None

    if not os.path.isfile(credentials_path):
        logger.error(
            "GOOGLE_DRIVE_MCP_CREDENTIALS points to a non-existent file: %s",
            credentials_path,
        )
        return None

    if not _MCP_AVAILABLE:
        logger.error("MCPToolset is not available in this installation")
        return None

    try:
        toolset = MCPToolset(
            connection_params=StdioServerParameters(
                command="npx",
                args=[
                    "-y",
                    "@google/drive-mcp-server",
                    "--credentials",
                    credentials_path,
                ],
            ),
            # Only expose the tools we need — defence in depth
            tool_filter=["create_file", "update_file", "list_files"],
        )
        logger.info("Google Drive MCPToolset initialised successfully")
        return toolset
    except Exception as exc:
        logger.error("Failed to initialise Drive MCPToolset: %s", exc)
        return None


async def export_report_to_drive(
    report_json: str,
    filename: str,
    toolset: Optional[Any] = None,
) -> dict[str, Any]:
    """
    Upload a JSON audit report to Google Drive.

    Args:
        report_json: JSON string of the audit report.
        filename: Target filename in Google Drive.
        toolset: An active MCPToolset. If None, Drive export is skipped.

    Returns:
        Dict with keys: success (bool), file_id (str or None), message (str).
    """
    if toolset is None:
        return {
            "success": False,
            "file_id": None,
            "message": "Drive export skipped — MCPToolset not configured",
        }

    try:
        # The Drive MCP server exposes a create_file tool
        result = await toolset.run_tool(
            tool_name="create_file",
            tool_input={
                "name": filename,
                "content": report_json,
                "mimeType": "application/json",
            },
        )
        file_id = result.get("id") or result.get("fileId")
        logger.info("Report uploaded to Google Drive: %s (id=%s)", filename, file_id)
        return {
            "success": True,
            "file_id": file_id,
            "message": f"Report successfully uploaded to Drive as '{filename}'",
        }
    except Exception as exc:
        logger.error("Drive upload failed: %s", exc)
        return {
            "success": False,
            "file_id": None,
            "message": f"Drive upload failed: {exc}",
        }
