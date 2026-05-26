"""
orchestrator_client.py — WebSocket + HTTP client for the google-docs-agent.

Registers with the orchestrator, handles Google Docs task_requests, and
applies settings pushed from the dashboard (credentials path, token path, etc.).
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import websockets
import websockets.exceptions

from gdocs_mcp_client import GDocsMCPClient, GDocsError

logger = logging.getLogger(__name__)

# ── Stable agent identity ──────────────────────────────────────────────────────

_AGENT_ID_FILE = Path(".agent_id")


def _stable_agent_id() -> str:
    if _AGENT_ID_FILE.exists():
        return _AGENT_ID_FILE.read_text().strip()
    new_id = str(uuid.uuid4())
    _AGENT_ID_FILE.write_text(new_id)
    logger.info("Generated new stable agent ID: %s", new_id)
    return new_id


# ── Registration payload ───────────────────────────────────────────────────────

AGENT_NAME        = "google-docs-agent"
AGENT_VERSION     = "1.0.0"
AGENT_DESCRIPTION = (
    "Google Docs agent powered by the Model Context Protocol (MCP). "
    "Searches, reads, creates, and updates Google Docs via the "
    "@modelcontextprotocol/server-gdrive MCP server for search/read and "
    "the Google Docs REST API for create/update operations."
)

REGISTRATION_PAYLOAD: dict = {
    "name":        AGENT_NAME,
    "description": AGENT_DESCRIPTION,
    "version":     AGENT_VERSION,
    "tags":        ["google", "docs", "drive", "documents", "mcp"],
    "capabilities": [
        {
            "name": "search_documents",
            "description": (
                "Search Google Drive for documents matching a text query. "
                "Returns file names, IDs, and URLs of matching documents."
            ),
            "tags": [
                "google", "docs", "drive", "search", "find", "locate",
                "lookup", "discover", "query", "list",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Full-text search query for Google Drive.",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results to return (1–50).",
                    },
                    "file_type": {
                        "type": "string",
                        "default": "document",
                        "enum": ["document", "spreadsheet", "presentation", "any"],
                        "description": "Filter by Google Workspace file type.",
                    },
                },
                "required": ["query"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "query":     {"type": "string"},
                    "file_type": {"type": "string"},
                    "results":   {"type": "array"},
                    "count":     {"type": "integer"},
                },
            },
        },
        {
            "name": "read_document",
            "description": (
                "Read the text content of a Google Doc. "
                "Provide either the file_id (Drive file ID) or file_name "
                "(searches Drive and reads the first match)."
            ),
            "tags": [
                "google", "docs", "drive", "read", "open", "get", "fetch",
                "content", "text", "view", "retrieve", "load",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Google Drive file ID of the document to read.",
                    },
                    "file_name": {
                        "type": "string",
                        "description": (
                            "Name or partial name of the document. Used to search "
                            "and read the first matching result when file_id is unknown."
                        ),
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "content": {"type": "string"},
                    "length":  {"type": "integer"},
                    "url":     {"type": "string"},
                },
            },
        },
        {
            "name": "create_document",
            "description": (
                "Create a new Google Doc with an optional initial body of text. "
                "Optionally share the document with other users (writer access)."
            ),
            "tags": [
                "google", "docs", "drive", "create", "new", "write", "make",
                "generate", "compose", "produce", "draft",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Title of the new Google Doc.",
                    },
                    "content": {
                        "type": "string",
                        "default": "",
                        "description": "Initial text content of the document.",
                    },
                    "share_with": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of email addresses to grant writer access. "
                            "Leave empty to keep the document private."
                        ),
                    },
                },
                "required": ["title"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "document_id":    {"type": "string"},
                    "title":          {"type": "string"},
                    "url":            {"type": "string"},
                    "content_length": {"type": "integer"},
                    "shared_with":    {"type": "array"},
                },
            },
        },
        {
            "name": "update_document",
            "description": (
                "Update an existing Google Doc. "
                "Use mode='append' to add content at the end, or "
                "mode='replace' to clear the document and insert new content."
            ),
            "tags": [
                "google", "docs", "drive", "update", "edit", "modify", "append",
                "replace", "rewrite", "change", "add", "insert", "write",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Google Drive file ID of the document to update.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to insert into the document.",
                    },
                    "mode": {
                        "type": "string",
                        "default": "append",
                        "enum": ["append", "replace"],
                        "description": (
                            "'append' adds content at the end; "
                            "'replace' clears the document and replaces it."
                        ),
                    },
                },
                "required": ["file_id", "content"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "document_id":    {"type": "string"},
                    "title":          {"type": "string"},
                    "url":            {"type": "string"},
                    "mode":           {"type": "string"},
                    "content_length": {"type": "integer"},
                },
            },
        },
    ],
    "required_settings": [
        {
            "key": "gdocs_credentials_file",
            "label": "OAuth Credentials File",
            "description": (
                "Path to the Google OAuth2 client secrets file (credentials.json) "
                "downloaded from the Google Cloud Console."
            ),
            "type": "string",
            "required": False,
            "default": "credentials.json",
        },
        {
            "key": "gdocs_token_file",
            "label": "OAuth Token File",
            "description": (
                "Path to the saved OAuth2 token file (token.json). "
                "Generated automatically by the OAuth setup flow."
            ),
            "type": "string",
            "required": False,
            "default": "token.json",
        },
        {
            "key": "gdocs_npx_path",
            "label": "npx Path",
            "description": (
                "Absolute path to the npx executable used to run the MCP server. "
                "Default 'npx' works when Node.js is on the system PATH."
            ),
            "type": "string",
            "required": False,
            "default": "npx",
        },
    ],
}

# ── Readiness ──────────────────────────────────────────────────────────────────
# Settings / files that MUST be present before this agent can accept traffic.
# For this agent readiness depends on physical OAuth credential files on disk
# (the paths themselves are configurable via settings).
# key → (human label, setup instruction)
_REQUIRED_SETTINGS: dict[str, tuple[str, str]] = {
    "credentials_file": (
        "Google OAuth Credentials File (credentials.json)",
        "Go to https://console.cloud.google.com → APIs & Services → Credentials, "
        "create an OAuth 2.0 Client ID (Desktop app), download the JSON, and save it as "
        "'credentials.json' in the agent directory (or the path set in gdocs_credentials_file). "
        "Then run 'python setup_oauth.py' to complete the OAuth flow and generate token.json.",
    ),
    "token_file": (
        "Google OAuth Token File (token.json)",
        "Run 'python setup_oauth.py' in the agent directory. This opens a browser, "
        "asks you to sign in with Google, and saves token.json automatically. "
        "You only need to do this once — the token refreshes on its own.",
    ),
}

# ── Constants ──────────────────────────────────────────────────────────────────

HEARTBEAT_INTERVAL_S: int = 15
MAX_BACKOFF_S:        int = 60
DRAIN_TIMEOUT_S:      int = 30

# Capability name → GDocsMCPClient method name
_CAPABILITY_MAP: dict[str, str] = {
    "search_documents": "search_documents",
    "read_document":    "read_document",
    "create_document":  "create_document",
    "update_document":  "update_document",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _envelope(
    sender_id:      str,
    msg_type:       str,
    payload:        dict,
    recipient_id:   Optional[str] = None,
    correlation_id: Optional[str] = None,
    msg_id:         Optional[str] = None,
) -> str:
    return json.dumps({
        "id":             msg_id or str(uuid.uuid4()),
        "type":           msg_type,
        "sender_id":      sender_id,
        "recipient_id":   recipient_id,
        "payload":        payload,
        "timestamp":      _now_iso(),
        "correlation_id": correlation_id,
    })


# ── Main client ────────────────────────────────────────────────────────────────

class OrchestratorClient:
    """Registers the google-docs-agent and handles incoming task_requests."""

    def __init__(self, orchestrator_url: str = "http://localhost:8000") -> None:
        self._base = orchestrator_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=30)

        self._agent_id:       str = ""
        self._ws_url:         str = ""
        self._common_settings: dict[str, Any] = {}

        self._status:           str   = "starting"
        self._active_tasks:     int   = 0
        self._tasks_completed:  int   = 0
        self._tasks_failed:     int   = 0
        self._total_duration_ms: float = 0.0
        self._start_time:       float = time.monotonic()

        self._shutting_down: bool    = False
        self._current_ws:    Any     = None

        # Readiness: keys of _REQUIRED_SETTINGS that are not yet satisfied
        self._missing_required: list[str] = list(_REQUIRED_SETTINGS.keys())

        self._gdocs = GDocsMCPClient()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._graceful_shutdown()))

        await self._register()
        await self._gdocs.start()
        await self._connect_loop()

    # ── Registration ───────────────────────────────────────────────────────────

    async def _register(self) -> None:
        url = f"{self._base}/api/v1/agents/register"
        logger.info("Registering with orchestrator at %s ...", url)
        payload = {**REGISTRATION_PAYLOAD, "id": _stable_agent_id()}
        resp = await self._http.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        self._agent_id = data["agent_id"]
        self._ws_url   = data["ws_url"]
        self._common_settings = {
            **data.get("common_settings", {}),
            **data.get("agent_settings", {}),
        }
        self._gdocs.update_settings(self._common_settings)
        self._check_readiness()
        logger.info("Registered — agent_id=%s  ws=%s", self._agent_id, self._ws_url)

    # ── WebSocket loop ─────────────────────────────────────────────────────────

    async def _connect_loop(self) -> None:
        backoff = 1.0
        while not self._shutting_down:
            try:
                logger.info("Connecting to %s ...", self._ws_url)
                async with websockets.connect(self._ws_url) as ws:
                    backoff = 1.0
                    await self._run_session(ws)

            except websockets.exceptions.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                if code == 4004:
                    logger.warning("Unknown agent_id (4004) — re-registering ...")
                    try:
                        await self._register()
                    except Exception as reg_exc:
                        logger.error("Re-registration failed: %s", reg_exc)
                elif code == 4003:
                    logger.info("Agent disabled (4003) — will retry")
                    backoff = max(backoff, 10.0)
                elif self._shutting_down:
                    break
                else:
                    logger.warning("WS closed (code=%s) — retry in %.0fs", code, backoff)

            except (OSError, Exception) as exc:
                if self._shutting_down:
                    break
                logger.warning("WS error (%s) — retry in %.0fs", exc, backoff)

            if not self._shutting_down:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    # ── Readiness helpers ──────────────────────────────────────────────────────

    def _check_readiness(self) -> None:
        """Check whether the required OAuth credential files exist on disk."""
        creds_path  = Path(self._common_settings.get("gdocs_credentials_file", "credentials.json"))
        token_path  = Path(self._common_settings.get("gdocs_token_file", "token.json"))

        missing: list[str] = []
        if not creds_path.exists():
            missing.append("credentials_file")
        if not token_path.exists():
            missing.append("token_file")

        was_unready = bool(self._missing_required)
        self._missing_required = missing

        if missing:
            labels = [_REQUIRED_SETTINGS[k][0] for k in missing]
            logger.warning(
                "Agent NOT READY — missing OAuth files: %s. "
                "Run 'python setup_oauth.py' to complete the setup.",
                ", ".join(labels),
            )
            # Reflect the error state immediately if a WS session is active
            self._status = "error"
        elif was_unready:
            logger.info("All required OAuth files are present — agent is READY")
            if self._current_ws:
                self._status = "available"

    def _not_ready_payload(self) -> dict:
        """Build the structured error payload returned to callers when not ready."""
        details = []
        for key in self._missing_required:
            label, instruction = _REQUIRED_SETTINGS[key]
            details.append({"setting": key, "label": label, "how_to_set": instruction})
        return {
            "error_code": "AGENT_NOT_READY",
            "message": (
                f"Agent '{AGENT_NAME}' is not ready to accept traffic. "
                f"The following required OAuth files are missing: "
                f"{', '.join(_REQUIRED_SETTINGS[k][0] for k in self._missing_required)}."
            ),
            "missing_settings": details,
            "resolution": (
                "1. Download credentials.json from the Google Cloud Console "
                "(APIs & Services → Credentials → OAuth 2.0 Client IDs). "
                "2. Run 'python setup_oauth.py' in the agent directory to generate token.json. "
                "The agent will automatically transition to 'available' once both files are present."
            ),
        }

    # ── WebSocket session ──────────────────────────────────────────────────────

    async def _run_session(self, ws) -> None:
        self._current_ws = ws
        self._status     = "error" if self._missing_required else "available"
        logger.info("WebSocket session active (status=%s)", self._status)
        try:
            await asyncio.gather(
                self._heartbeat_loop(ws),
                self._recv_loop(ws),
            )
        finally:
            self._current_ws = None
            self._status     = "offline"

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            hb_payload: dict[str, Any] = {
                "status":       self._status,
                "current_load": min(self._active_tasks / 5.0, 1.0),
                "active_tasks": self._active_tasks,
                "metrics":      self._metrics(),
            }
            if self._missing_required:
                hb_payload["error_message"] = (
                    f"Missing required OAuth files: "
                    f"{', '.join(_REQUIRED_SETTINGS[k][0] for k in self._missing_required)}"
                )
            await self._ws_send(ws, self._msg("heartbeat", hb_payload))
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    # ── Receive loop ───────────────────────────────────────────────────────────

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Non-JSON frame ignored")
                continue
            mtype = msg.get("type", "?")
            _lvl = logging.DEBUG if mtype in ("agent_registered", "agent_offline", "heartbeat_ack", "settings_push") else logging.INFO
            logger.log(_lvl, "← [%s] from=%s", mtype, msg.get("sender_id", "?"))
            await self._dispatch(ws, msg)

    async def _dispatch(self, ws, msg: dict) -> None:
        mtype   = msg.get("type", "")
        payload = msg.get("payload", {})

        if mtype == "task_request":
            asyncio.create_task(self._handle_task(ws, msg))

        elif mtype == "settings_push":
            pushed = payload.get("settings", {})
            self._common_settings.update(pushed)
            self._gdocs.update_settings(self._common_settings)
            logger.info("Settings updated via push: %s", list(pushed.keys()))
            self._check_readiness()

        elif mtype in ("agent_registered", "agent_offline"):
            logger.debug("Peer event [%s]: %s", mtype, payload.get("agent_id"))

        elif mtype == "error":
            logger.error(
                "Orchestrator error [%s]: %s",
                payload.get("code"), payload.get("detail"),
            )

        else:
            logger.debug("Unhandled message type: %r", mtype)

    # ── Task handling ──────────────────────────────────────────────────────────

    async def _handle_task(self, ws, msg: dict) -> None:
        # Refuse traffic immediately if required OAuth files are missing.
        if self._missing_required:
            await self._ws_send(ws, self._msg(
                "task_response",
                {
                    "success": False,
                    "error": (
                        f"AGENT_NOT_READY: {AGENT_NAME} cannot accept traffic until "
                        f"required OAuth files are present. "
                        f"Missing: {', '.join(_REQUIRED_SETTINGS[k][0] for k in self._missing_required)}."
                    ),
                    "output_data": self._not_ready_payload(),
                    "duration_ms": 0,
                },
                recipient_id=msg.get("sender_id"),
                correlation_id=msg.get("id"),
            ))
            logger.warning(
                "Rejected task (capability=%s) — agent not ready",
                msg.get("payload", {}).get("capability"),
            )
            return

        req_id     = msg.get("id")
        sender_id  = msg.get("sender_id")
        payload    = msg.get("payload", {})
        capability = payload.get("capability", "")
        input_data = payload.get("input_data", {})

        self._active_tasks += 1
        self._status        = "busy"
        t0 = time.monotonic()

        try:
            output, error = await self._dispatch_capability(capability, input_data)
            duration_ms   = (time.monotonic() - t0) * 1000

            if error:
                self._tasks_failed += 1
                await self._ws_send(ws, self._msg(
                    "task_response",
                    {"success": False, "error": error, "duration_ms": round(duration_ms, 1)},
                    recipient_id=sender_id,
                    correlation_id=req_id,
                ))
            else:
                self._tasks_completed  += 1
                self._total_duration_ms += duration_ms
                await self._ws_send(ws, self._msg(
                    "task_response",
                    {"success": True, "output_data": output, "duration_ms": round(duration_ms, 1)},
                    recipient_id=sender_id,
                    correlation_id=req_id,
                ))

        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            self._tasks_failed += 1
            logger.exception("Unhandled error in capability %r", capability)
            await self._ws_send(ws, self._msg(
                "task_response",
                {"success": False, "error": str(exc), "duration_ms": round(duration_ms, 1)},
                recipient_id=sender_id,
                correlation_id=req_id,
            ))

        finally:
            self._active_tasks = max(0, self._active_tasks - 1)
            self._status       = "draining" if self._shutting_down else (
                "busy" if self._active_tasks else "available"
            )
            await self._send_status_update(ws)

    async def _dispatch_capability(
        self, capability: str, input_data: dict
    ) -> tuple[Optional[dict], Optional[str]]:
        method_name = _CAPABILITY_MAP.get(capability)
        if not method_name:
            return None, f"Unknown capability: {capability!r}"

        method = getattr(self._gdocs, method_name)
        clean_input = {k: v for k, v in input_data.items() if not k.startswith("_")}
        try:
            result = await method(**clean_input)
            return result, None
        except GDocsError as exc:
            return None, str(exc)
        except TypeError as exc:
            return None, f"Invalid input for {capability!r}: {exc}"
        except Exception as exc:
            logger.exception("Unexpected error in %r", capability)
            return None, f"Unexpected error: {exc}"

    # ── Status update ──────────────────────────────────────────────────────────

    async def _send_status_update(self, ws) -> None:
        await self._ws_send(ws, self._msg(
            "status_update",
            {
                "status":       self._status,
                "current_load": min(self._active_tasks / 5.0, 1.0),
                "active_tasks": self._active_tasks,
                "metrics":      self._metrics(),
            },
        ))

    # ── Graceful shutdown ──────────────────────────────────────────────────────

    async def _graceful_shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Shutdown signal — draining ...")
        self._status = "draining"

        deadline = time.monotonic() + DRAIN_TIMEOUT_S
        while self._active_tasks > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.5)

        await self._gdocs.stop()

        if self._agent_id:
            try:
                await self._http.delete(f"{self._base}/api/v1/agents/{self._agent_id}")
                logger.info("Deregistered from orchestrator.")
            except Exception as exc:
                logger.warning("Deregister failed: %s", exc)

        await self._http.aclose()
        logger.info("Shutdown complete.")

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _ws_send(self, ws, msg_str: str) -> None:
        msg   = json.loads(msg_str)
        mtype = msg.get("type", "?")
        noisy = mtype in ("heartbeat", "status_update")
        (logger.debug if noisy else logger.info)(
            "→ [%s] to=%s", mtype, msg.get("recipient_id") or "orchestrator"
        )
        try:
            await ws.send(msg_str)
        except websockets.exceptions.ConnectionClosed:
            raise
        except Exception as exc:
            logger.warning("WS send failed: %s", exc)

    def _msg(
        self,
        msg_type:       str,
        payload:        dict,
        recipient_id:   Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> str:
        return _envelope(self._agent_id, msg_type, payload, recipient_id, correlation_id)

    def _metrics(self) -> dict:
        n = self._tasks_completed + self._tasks_failed
        return {
            "tasks_completed":      self._tasks_completed,
            "tasks_failed":         self._tasks_failed,
            "avg_response_time_ms": round(self._total_duration_ms / n, 1) if n else 0.0,
            "uptime_seconds":       round(time.monotonic() - self._start_time, 1),
        }
