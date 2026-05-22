"""
setup_oauth.py — One-time OAuth2 authorisation flow for the google-docs-agent.

Run this script once to generate token.json before starting the agent.
Requires credentials.json (OAuth2 client secrets) from the Google Cloud Console.

Usage:
    python setup_oauth.py [--credentials credentials.json] [--token token.json]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Google OAuth2 setup for google-docs-agent")
    p.add_argument("--credentials", default="credentials.json",
                   help="Path to credentials.json (OAuth2 client secrets)")
    p.add_argument("--token", default="token.json",
                   help="Where to save the resulting token.json")
    return p.parse_args()


def run_flow(credentials_file: str, token_file: str) -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
    except ImportError:
        print(
            "ERROR: Missing packages. Install with:\n"
            "  pip install google-auth-oauthlib google-auth-httplib2",
            file=sys.stderr,
        )
        sys.exit(1)

    creds_path = Path(credentials_file)
    if not creds_path.exists():
        print(
            f"ERROR: {credentials_file} not found.\n"
            "Download it from Google Cloud Console:\n"
            "  1. https://console.cloud.google.com/apis/credentials\n"
            "  2. Create OAuth 2.0 Client ID → Desktop App\n"
            "  3. Download JSON and save as credentials.json",
            file=sys.stderr,
        )
        sys.exit(1)

    SCOPES = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    print("Opening browser for Google OAuth2 authorisation ...")
    creds = flow.run_local_server(port=0)

    token_path = Path(token_file)
    token_path.write_text(creds.to_json())
    print(f"Token saved to {token_path.resolve()}")
    print("You can now start the agent with: python main.py")


if __name__ == "__main__":
    args = parse_args()
    run_flow(args.credentials, args.token)
