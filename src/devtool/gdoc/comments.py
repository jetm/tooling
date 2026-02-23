"""devtool gdoc-comments — Google Docs comment fetcher."""

import re
import sys
from datetime import datetime
from pathlib import Path

import click

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CONFIG_DIR = Path.home() / ".config" / "devtool"
CREDENTIALS_PATH = CONFIG_DIR / "gdoc_credentials.json"
TOKEN_PATH = CONFIG_DIR / "gdoc_token.json"

COMMENT_FIELDS = (
    "comments(id,content,htmlContent,author/displayName,"
    "quotedFileContent,resolved,deleted,createdTime,"
    "modifiedTime,replies(id,content,htmlContent,author/displayName,"
    "createdTime,action,deleted)),"
    "nextPageToken"
)

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


def fetch_comments(file_id: str, creds) -> list[dict]:
    """Fetch all comments from a Google Docs document."""
    from googleapiclient.discovery import build

    service = build("drive", "v3", credentials=creds)
    comments = []

    request = service.comments().list(
        fileId=file_id,
        fields=COMMENT_FIELDS,
        pageSize=100,
        includeDeleted=False,
    )

    while request is not None:
        response = request.execute()
        comments.extend(response.get("comments", []))
        request = service.comments().list_next(request, response)

    return comments


def format_timestamp(ts: str) -> str:
    """Format an RFC 3339 timestamp for display."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d %H:%M")


def format_comment(comment: dict, index: int) -> str:
    """Format a single comment with its replies as plain text."""
    lines = []
    separator = "-" * 72

    # Status indicator
    status = "[RESOLVED]" if comment.get("resolved") else "[OPEN]"

    # Header
    author = comment.get("author", {}).get("displayName", "Unknown")
    created = format_timestamp(comment.get("createdTime", ""))
    lines.append(f"Comment #{index} {status}")
    lines.append(separator)
    lines.append(f"Author: {author}")
    lines.append(f"Date:   {created}")

    # Quoted context
    quoted = comment.get("quotedFileContent", {})
    if quoted and quoted.get("value"):
        lines.append("")
        lines.append("Context:")
        for ctx_line in quoted["value"].splitlines():
            lines.append(f"  > {ctx_line}")

    # Comment body
    lines.append("")
    content = comment.get("content", "")
    for body_line in content.splitlines():
        lines.append(f"  {body_line}")

    # Replies
    replies = comment.get("replies", [])
    active_replies = [r for r in replies if not r.get("deleted")]
    if active_replies:
        lines.append("")
        lines.append(f"  Replies ({len(active_replies)}):")
        for reply in active_replies:
            reply_author = reply.get("author", {}).get("displayName", "Unknown")
            reply_created = format_timestamp(reply.get("createdTime", ""))
            reply_content = reply.get("content", "")
            action = reply.get("action", "")

            if action == "resolve":
                lines.append(f"    [{reply_author} resolved this — {reply_created}]")
            elif action == "reopen":
                lines.append(f"    [{reply_author} reopened this — {reply_created}]")
            else:
                lines.append(f"    {reply_author} ({reply_created}):")
                for reply_line in reply_content.splitlines():
                    lines.append(f"      {reply_line}")

    lines.append("")
    return "\n".join(lines)


@click.command()
@click.argument("document")
@click.option("--all", "show_all", is_flag=True, help="Show all comments including resolved")
@click.option("--resolved", "show_resolved", is_flag=True, help="Show only resolved comments")
@click.option("--output", "-o", type=click.Path(), help="Write output to file instead of stdout")
def gdoc_comments(document: str, show_all: bool, show_resolved: bool, output: str | None) -> None:
    """Show comments from a Google Docs document.

    DOCUMENT can be a full Google Docs URL or a file ID.
    By default, only open (unresolved) comments are shown.
    """
    file_id = extract_file_id(document)
    creds = authenticate()
    comments = fetch_comments(file_id, creds)

    if not comments:
        click.echo("No comments found.", err=True)
        sys.exit(0)

    # Filter comments
    if show_resolved:
        filtered = [c for c in comments if c.get("resolved")]
    elif show_all:
        filtered = comments
    else:
        filtered = [c for c in comments if not c.get("resolved")]

    if not filtered:
        if show_resolved:
            click.echo("No resolved comments found.", err=True)
        else:
            click.echo("No open comments found. Use --all to see resolved comments.", err=True)
        sys.exit(0)

    # Build output
    header = "=" * 72
    parts = [
        header,
        f"GOOGLE DOCS COMMENTS ({len(filtered)} of {len(comments)} total)",
        header,
        "",
    ]

    for i, comment in enumerate(filtered, 1):
        parts.append(format_comment(comment, i))

    result = "\n".join(parts)

    if output:
        Path(output).write_text(result)
        click.echo(f"Output written to {output}", err=True)
    else:
        click.echo(result)
