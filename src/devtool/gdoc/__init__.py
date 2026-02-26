"""Shared authentication and utilities for Google Docs commands."""

import re
import sys
from pathlib import Path

import click

SCOPES = ["https://www.googleapis.com/auth/drive"]
CONFIG_DIR = Path.home() / ".config" / "devtool"
CREDENTIALS_PATH = CONFIG_DIR / "gdoc_credentials.json"
TOKEN_PATH = CONFIG_DIR / "gdoc_token.json"

DOC_URL_PATTERN = re.compile(r"https://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)")


def extract_file_id(doc_ref: str) -> str:
    """Extract Google Docs file ID from a URL or return as-is if already an ID."""
    match = DOC_URL_PATTERN.search(doc_ref)
    if match:
        return match.group(1)
    # Assume it's a raw file ID
    if re.match(r"^[a-zA-Z0-9_-]+$", doc_ref):
        return doc_ref
    click.echo(f"Error: cannot parse document reference: {doc_ref}", err=True)
    sys.exit(1)


def authenticate():
    """Authenticate with Google Drive API using OAuth2 installed app flow."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CREDENTIALS_PATH.exists():
        click.echo(
            f"Error: OAuth credentials not found at {CREDENTIALS_PATH}\n\n"
            "Setup instructions:\n"
            "  1. Go to https://console.cloud.google.com/\n"
            "  2. Enable the Google Drive API\n"
            "  3. Create OAuth 2.0 credentials (Desktop application)\n"
            f"  4. Download and save as {CREDENTIALS_PATH}",
            err=True,
        )
        sys.exit(1)

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        # Check if token has required scopes - force re-auth if scope was upgraded
        if creds.scopes and not set(SCOPES).issubset(set(creds.scopes)):
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())

    return creds


def build_drive_service(creds):
    """Build a Google Drive API v3 service client."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds)
