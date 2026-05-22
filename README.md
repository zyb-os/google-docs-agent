# google-docs-agent

A Google Docs agent that connects to the agent orchestrator and can search, read,
create, and update Google Docs documents.

**Architecture:**
- **Search + Read** — via the [`@modelcontextprotocol/server-gdrive`](https://github.com/modelcontextprotocol/servers/tree/main/src/gdrive) MCP server (Node.js subprocess, stdio transport)
- **Create + Update** — via the Google Docs REST API (Python `google-api-python-client`)
- **Orchestrator** — standard WebSocket connection using the existing agent protocol

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | |
| Node.js 18+ | Required to run the MCP server via `npx` |
| Google Cloud project | With Google Docs API and Google Drive API enabled |

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Install the MCP server (optional pre-fetch)

The MCP server is run via `npx -y @modelcontextprotocol/server-gdrive` and is
fetched automatically on first start. To pre-install:

```bash
npm install -g @modelcontextprotocol/server-gdrive
```

### 3. Configure Google Cloud credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create or select a project
3. Enable **Google Docs API** and **Google Drive API**
4. Navigate to **APIs & Services → Credentials**
5. Create an **OAuth 2.0 Client ID** (Application type: **Desktop App**)
6. Download the JSON file and save it as `credentials.json` in this directory

### 4. Run the OAuth authorisation flow

```bash
python setup_oauth.py
```

This opens a browser, prompts you to sign in with Google, and saves `token.json`.
You only need to do this once (the token is refreshed automatically when it expires).

### 5. Start the agent

```bash
python main.py
# or with a custom orchestrator URL:
python main.py --orchestrator-url http://localhost:8000
```

---

## Capabilities

| Capability | Description |
|---|---|
| `search_documents` | Full-text search across Google Drive. Filter by `file_type`: `document`, `spreadsheet`, `presentation`, or `any`. |
| `read_document` | Read document content by `file_id` or `file_name` (auto-searches Drive). |
| `create_document` | Create a new Google Doc with a title, optional content, and optional sharing. |
| `update_document` | Append to or replace the content of an existing Google Doc. |

---

## Dashboard Settings

These can be configured from the orchestrator dashboard under the agent's settings panel:

| Key | Default | Description |
|---|---|---|
| `gdocs_credentials_file` | `credentials.json` | Path to the OAuth2 client secrets file |
| `gdocs_token_file` | `token.json` | Path to the saved OAuth token |
| `gdocs_npx_path` | `npx` | Path to the `npx` executable (override if not on PATH) |

---

## How MCP is used

The agent spawns `@modelcontextprotocol/server-gdrive` as a long-lived subprocess
on startup and communicates over stdio using the Model Context Protocol:

```
google-docs-agent
    │
    ├── stdio ──► @modelcontextprotocol/server-gdrive (Node.js)
    │                 └── Google Drive API (search, read)
    │
    └── REST ───► Google Docs API (create, update)
```

The MCP session is kept alive for the lifetime of the agent process (no per-request
subprocess spawning). If the MCP server fails to start (e.g. Node.js not installed),
`search_documents` and `read_document` will return an error while `create_document`
and `update_document` continue to work normally.

---

## Troubleshooting

**`mcp package not installed`** — Run `pip install mcp`

**`MCP server failed to start`** — Ensure Node.js 18+ is installed and `npx` is on PATH.
Test with: `npx -y @modelcontextprotocol/server-gdrive`

**`token.json not found`** — Run `python setup_oauth.py` to complete the OAuth flow.

**`Token expired`** — The agent refreshes tokens automatically using `refresh_token`.
If refresh fails, re-run `python setup_oauth.py`.

**`403 Forbidden` on create/update** — The authenticated Google account must have
edit access to the document. For newly created docs, the creator always has access.
