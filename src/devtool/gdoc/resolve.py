"""devtool gdoc-resolve - Resolve all open comments on a Google Doc."""

import sys

import click

from devtool.gdoc import authenticate, build_drive_service, extract_file_id

COMMENT_FIELDS = "comments(id,content,resolved,author/displayName,quotedFileContent),nextPageToken"


def fetch_open_comments(service, file_id: str) -> list[dict]:
    """Fetch all open (unresolved) comments from a Google Doc."""
    comments = []
    request = service.comments().list(
        fileId=file_id,
        fields=COMMENT_FIELDS,
        pageSize=100,
        includeDeleted=False,
    )
    while request is not None:
        response = request.execute()
        for comment in response.get("comments", []):
            if not comment.get("resolved"):
                comments.append(comment)
        request = service.comments().list_next(request, response)
    return comments


def resolve_comment(service, file_id: str, comment_id: str) -> None:
    """Resolve a single comment by creating a reply with action=resolve."""
    service.replies().create(
        fileId=file_id,
        commentId=comment_id,
        fields="id",
        body={"action": "resolve", "content": ""},
    ).execute()


@click.command()
@click.argument("document")
@click.option("--dry-run", is_flag=True, help="List open comments without resolving them")
def gdoc_resolve(document: str, dry_run: bool) -> None:
    """Resolve all open comments on a Google Docs document.

    DOCUMENT can be a full Google Docs URL or a file ID.
    """
    file_id = extract_file_id(document)
    creds = authenticate()
    service = build_drive_service(creds)

    open_comments = fetch_open_comments(service, file_id)

    if not open_comments:
        click.echo("No open comments to resolve.")
        sys.exit(0)

    if dry_run:
        click.echo(f"Found {len(open_comments)} open comment(s) that would be resolved:\n")
        for i, comment in enumerate(open_comments, 1):
            author = comment.get("author", {}).get("displayName", "Unknown")
            content = comment.get("content", "").split("\n")[0]
            quoted = comment.get("quotedFileContent", {}).get("value", "")
            if quoted:
                quoted = quoted.split("\n")[0]
                click.echo(f'  {i}. [{author}] on "{quoted}"')
            else:
                click.echo(f"  {i}. [{author}] {content}")
        return

    with click.progressbar(open_comments, label="Resolving comments") as bar:
        for comment in bar:
            resolve_comment(service, file_id, comment["id"])

    click.echo(f"Resolved {len(open_comments)} comment(s).")
