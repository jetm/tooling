"""devtool gdoc-comments - Google Docs comment fetcher."""

import sys
from datetime import datetime
from pathlib import Path

import click

from devtool.gdoc import authenticate, build_drive_service, extract_file_id

COMMENT_FIELDS = (
    "comments(id,content,htmlContent,author/displayName,"
    "quotedFileContent,resolved,deleted,createdTime,"
    "modifiedTime,replies(id,content,htmlContent,author/displayName,"
    "createdTime,action,deleted)),"
    "nextPageToken"
)


def fetch_comments(file_id: str, creds) -> list[dict]:
    """Fetch all comments from a Google Docs document."""
    service = build_drive_service(creds)
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
                lines.append(f"    [{reply_author} resolved this - {reply_created}]")
            elif action == "reopen":
                lines.append(f"    [{reply_author} reopened this - {reply_created}]")
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
