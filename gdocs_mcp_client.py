"""
gdocs_mcp_client.py — Google Docs operations via MCP + Google Docs REST API.

MCP (@modelcontextprotocol/server-gdrive) handles search and read.
The Google Docs/Drive REST API handles create and update operations.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Optional MCP imports ───────────────────────────────────────────────────────

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    logger.warning("mcp package not installed — search/read via MCP disabled")

# ── Optional Google API imports ────────────────────────────────────────────────

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False
    logger.warning("google-api-python-client not installed — create/update disabled")

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
]

_MIME_TYPES = {
    "document":     "application/vnd.google-apps.document",
    "spreadsheet":  "application/vnd.google-apps.spreadsheet",
    "presentation": "application/vnd.google-apps.presentation",
}


class GDocsError(Exception):
    """Raised for recoverable Google Docs errors."""


class GDocsMCPClient:
    """
    Wraps the @modelcontextprotocol/server-gdrive MCP server for search/read
    and uses the Google Docs REST API directly for create/update operations.
    """

    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
        npx_path: str = "npx",
    ) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._npx_path = npx_path

        # MCP session handles
        self._stdio_ctx: Any = None
        self._session_ctx: Any = None
        self._session: Optional[Any] = None
        self._mcp_ready: bool = False

        # Google REST API service objects
        self._docs_service: Any = None
        self._drive_service: Any = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the MCP server subprocess and initialise Google API clients."""
        await self._start_mcp()
        await asyncio.to_thread(self._init_google_api)

    async def stop(self) -> None:
        """Gracefully stop the MCP server subprocess."""
        if self._session_ctx:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug("MCP session close error: %s", exc)
            self._session_ctx = None
            self._session = None

        if self._stdio_ctx:
            try:
                await self._stdio_ctx.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug("MCP stdio close error: %s", exc)
            self._stdio_ctx = None

        self._mcp_ready = False

    async def _start_mcp(self) -> None:
        """Spawn the Google Drive MCP server and open a long-lived session."""
        if not MCP_AVAILABLE:
            logger.warning("mcp package missing — install with: pip install mcp")
            return

        env = {
            **os.environ,
            # The server picks up OAuth client secrets from this path
            "GDRIVE_CREDENTIALS_PATH": os.path.abspath(self._token_file),
        }

        server_params = StdioServerParameters(
            command=self._npx_path,
            args=["-y", "@modelcontextprotocol/server-gdrive"],
            env=env,
        )

        try:
            self._stdio_ctx = stdio_client(server_params)
            read, write = await self._stdio_ctx.__aenter__()
            self._session_ctx = ClientSession(read, write)
            self._session = await self._session_ctx.__aenter__()
            await self._session.initialize()
            self._mcp_ready = True

            tools_result = await self._session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            logger.info("MCP Google Drive server ready. Tools: %s", tool_names)

        except Exception as exc:
            logger.error("Failed to start MCP server: %s", exc)
            self._mcp_ready = False

    def _init_google_api(self) -> None:
        """Initialise Google Docs and Drive API clients from token.json."""
        if not GOOGLE_API_AVAILABLE:
            logger.warning("google-api-python-client not installed")
            return

        token_path = Path(self._token_file)
        if not token_path.exists():
            logger.warning(
                "Token file %s not found. Run the OAuth setup flow first (see README).",
                self._token_file,
            )
            return

        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                token_path.write_text(creds.to_json())
                logger.info("OAuth token refreshed and saved to %s", self._token_file)

            self._docs_service = build("docs", "v1", credentials=creds)
            self._drive_service = build("drive", "v3", credentials=creds)
            logger.info("Google Docs API client initialised")

        except Exception as exc:
            logger.error("Failed to initialise Google API client: %s", exc)

    def update_settings(self, settings: dict) -> None:
        """Apply settings pushed from the orchestrator dashboard."""
        creds_file = settings.get("gdocs_credentials_file", self._credentials_file)
        token_file = settings.get("gdocs_token_file", self._token_file)
        npx_path   = settings.get("gdocs_npx_path", self._npx_path)

        api_changed = (
            creds_file != self._credentials_file or token_file != self._token_file
        )
        self._credentials_file = creds_file
        self._token_file       = token_file
        self._npx_path         = npx_path

        if api_changed:
            self._init_google_api()

    # ── Search ────────────────────────────────────────────────────────────────

    async def search_documents(
        self,
        query: str,
        max_results: int = 10,
        file_type: str = "document",
    ) -> dict:
        """
        Search Google Drive for files matching the query via the MCP server.

        file_type: "document" | "spreadsheet" | "presentation" | "any"
        """
        if not self._mcp_ready or not self._session:
            raise GDocsError(
                "MCP server not available. Ensure Node.js and the Google Drive MCP "
                "server are installed, and that token.json exists."
            )

        # Build a Drive query understood by the MCP server
        mime_filter = ""
        if file_type in _MIME_TYPES:
            mime_filter = f" and mimeType='{_MIME_TYPES[file_type]}'"

        drive_query = f"fullText contains '{query}' and trashed=false{mime_filter}"

        try:
            result = await self._session.call_tool("search", {"query": drive_query})
        except Exception as exc:
            raise GDocsError(f"MCP search failed: {exc}") from exc

        # The MCP server returns TextContent items; content may be JSON or plain text
        items: list[dict] = []
        for content in result.content:
            if not hasattr(content, "text"):
                continue
            try:
                parsed = json.loads(content.text)
                if isinstance(parsed, list):
                    items.extend(parsed)
                elif isinstance(parsed, dict):
                    items.append(parsed)
            except json.JSONDecodeError:
                items.append({"snippet": content.text})

        items = items[:max_results]
        return {
            "query": query,
            "file_type": file_type,
            "results": items,
            "count": len(items),
        }

    # ── Read ──────────────────────────────────────────────────────────────────

    async def read_document(
        self,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> dict:
        """
        Read a Google Doc's content via the MCP server.

        Provide either file_id (Drive file ID) or file_name (searches Drive and
        reads the first matching document).
        """
        if not self._mcp_ready or not self._session:
            raise GDocsError("MCP server not available")

        if not file_id and not file_name:
            raise GDocsError("Provide either file_id or file_name")

        # Resolve name → id via search
        if not file_id:
            search_result = await self.search_documents(
                query=file_name,  # type: ignore[arg-type]
                max_results=1,
                file_type="document",
            )
            if not search_result["results"]:
                raise GDocsError(f"No document found with name matching '{file_name}'")
            first = search_result["results"][0]
            file_id = first.get("id") or first.get("fileId") or first.get("file_id")
            if not file_id:
                raise GDocsError(
                    f"Could not determine file ID from search result: {first}"
                )

        try:
            result = await self._session.call_tool("read_file", {"fileId": file_id})
        except Exception as exc:
            raise GDocsError(f"MCP read_file failed: {exc}") from exc

        content_parts: list[str] = []
        for content in result.content:
            if hasattr(content, "text"):
                content_parts.append(content.text)

        full_content = "\n".join(content_parts)
        return {
            "file_id": file_id,
            "content": full_content,
            "length": len(full_content),
            "url": f"https://docs.google.com/document/d/{file_id}/edit",
        }

    # ── Create ────────────────────────────────────────────────────────────────

    async def create_document(
        self,
        title: str,
        content: str = "",
        share_with: Optional[list[str]] = None,
    ) -> dict:
        """
        Create a new Google Doc with the given title and optional initial content.
        Optionally share the doc with a list of email addresses (writer role).
        """
        if not self._docs_service:
            raise GDocsError(
                "Google Docs API not initialised. Ensure token.json exists (run OAuth "
                "setup flow) and google-api-python-client is installed."
            )

        try:
            # 1. Create an empty document
            doc = await asyncio.to_thread(
                self._docs_service.documents().create(body={"title": title}).execute
            )
            doc_id  = doc["documentId"]
            doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"

            # 2. Insert content if provided
            if content:
                requests = [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": content,
                        }
                    }
                ]
                await asyncio.to_thread(
                    self._docs_service.documents()
                    .batchUpdate(documentId=doc_id, body={"requests": requests})
                    .execute
                )

            # 3. Share with additional users
            shared: list[str] = []
            for email in (share_with or []):
                try:
                    await asyncio.to_thread(
                        self._drive_service.permissions()
                        .create(
                            fileId=doc_id,
                            body={
                                "type": "user",
                                "role": "writer",
                                "emailAddress": email,
                            },
                        )
                        .execute
                    )
                    shared.append(email)
                except Exception as exc:
                    logger.warning("Failed to share %s with %s: %s", doc_id, email, exc)

            logger.info("Created document '%s' (%s)", title, doc_id)
            return {
                "document_id": doc_id,
                "title": title,
                "url": doc_url,
                "content_length": len(content),
                "shared_with": shared,
            }

        except GDocsError:
            raise
        except Exception as exc:
            raise GDocsError(f"Failed to create document: {exc}") from exc

    # ── Update ────────────────────────────────────────────────────────────────

    async def update_document(
        self,
        file_id: str,
        content: str,
        mode: str = "append",
    ) -> dict:
        """
        Update an existing Google Doc.

        mode="append"  — insert content at the end of the document.
        mode="replace" — clear the document body and replace with new content.
        """
        if not self._docs_service:
            raise GDocsError("Google Docs API not initialised")

        if mode not in ("append", "replace"):
            raise GDocsError(f"Invalid mode '{mode}'. Must be 'append' or 'replace'")

        try:
            doc = await asyncio.to_thread(
                self._docs_service.documents().get(documentId=file_id).execute
            )
            title        = doc.get("title", "")
            body_content = doc.get("body", {}).get("content", [])
            end_index    = (body_content[-1]["endIndex"] - 1) if body_content else 1

            requests: list[dict] = []

            if mode == "replace" and end_index > 1:
                requests.append({
                    "deleteContentRange": {
                        "range": {"startIndex": 1, "endIndex": end_index}
                    }
                })
                requests.append({
                    "insertText": {
                        "location": {"index": 1},
                        "text": content,
                    }
                })
            else:
                # Append: insert at the current end (add a newline separator)
                insert_index = max(end_index, 1)
                insert_text  = ("\n" + content) if end_index > 1 else content
                requests.append({
                    "insertText": {
                        "location": {"index": insert_index},
                        "text": insert_text,
                    }
                })

            if requests:
                await asyncio.to_thread(
                    self._docs_service.documents()
                    .batchUpdate(documentId=file_id, body={"requests": requests})
                    .execute
                )

            doc_url = f"https://docs.google.com/document/d/{file_id}/edit"
            logger.info("Updated document '%s' (%s) mode=%s", title, file_id, mode)
            return {
                "document_id": file_id,
                "title": title,
                "url": doc_url,
                "mode": mode,
                "content_length": len(content),
            }

        except GDocsError:
            raise
        except Exception as exc:
            raise GDocsError(f"Failed to update document: {exc}") from exc
