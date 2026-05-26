# Agent Status: NOT READY — NEEDS ATTENTION

**Status:** ❌ NOT READY TO ACCEPT TRAFFIC
**Reason:** Google OAuth credential files are missing from disk — agent cannot authenticate with Google APIs

## Missing Files

| File | Status | Purpose |
|------|--------|---------|
| `credentials.json` | ❌ NOT FOUND | Google OAuth2 client secrets downloaded from Cloud Console |
| `token.json` | ❌ NOT FOUND | Saved OAuth token — generated automatically after first auth |

## How to Fix

### Step 1 — Create a Google Cloud project and enable the Docs API
1. Go to https://console.cloud.google.com
2. Create a new project (or select an existing one)
3. Enable the **Google Docs API** and **Google Drive API**

### Step 2 — Download OAuth credentials
1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → OAuth 2.0 Client ID**
3. Application type: **Desktop app**
4. Download the JSON file and save it as `credentials.json` in this directory

### Step 3 — Complete the OAuth flow
Run the setup script to generate `token.json`:

```bash
python setup_oauth.py
```

This opens a browser, prompts you to sign in with Google, and saves `token.json`.
You only need to do this once (the token refreshes automatically).

This agent will remain offline and refuse traffic until both OAuth files are present and valid.
