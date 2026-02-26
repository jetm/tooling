"""devtool gdoc-upload - Replace Google Doc content with a local Markdown file."""

from pathlib import Path

import click

from devtool.gdoc import authenticate, build_drive_service, extract_file_id


def upload_to_doc(service, file_id: str, local_path: Path, mime_type: str) -> None:
    """Replace Google Doc content by uploading a file."""
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
    service.files().update(
        fileId=file_id,
        media_body=media,
    ).execute()


@click.command()
@click.argument("document")
@click.argument("file", type=click.Path(exists=True))
def gdoc_upload(document: str, file: str) -> None:
    """Replace a Google Doc's content with a local Markdown file.

    DOCUMENT can be a full Google Docs URL or a file ID.
    FILE is the local markdown file to upload.

    Uploads the Markdown file directly using the Google Drive API's native
    Markdown import, which preserves formatting including code blocks.
    """
    source = Path(file)
    file_id = extract_file_id(document)

    creds = authenticate()
    service = build_drive_service(creds)

    click.echo("Uploading to Google Docs...")
    upload_to_doc(service, file_id, source, "text/markdown")

    doc_url = f"https://docs.google.com/document/d/{file_id}/edit"
    click.echo(f"Done. Document updated: {doc_url}")
