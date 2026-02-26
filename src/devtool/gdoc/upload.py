"""devtool gdoc-upload - Replace Google Doc content with a local file."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click

from devtool.gdoc import authenticate, build_drive_service, extract_file_id


def create_reference_docx() -> Path:
    """Create a reference DOCX with Google Docs default styles (Arial 11pt)."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Arial"
    font.size = Pt(11)

    ref_path = Path(tempfile.mkstemp(suffix=".docx")[1])
    doc.save(str(ref_path))
    return ref_path


def convert_to_docx(source: Path) -> Path:
    """Convert a file to DOCX using pandoc with Google Docs default font."""
    ref_path = create_reference_docx()
    tmp_path = Path(tempfile.mkstemp(suffix=".docx")[1])
    try:
        result = subprocess.run(
            ["pandoc", str(source), "--reference-doc", str(ref_path), "-o", str(tmp_path)],
            capture_output=True,
            text=True,
        )
    finally:
        ref_path.unlink(missing_ok=True)
    if result.returncode != 0:
        click.echo(f"Error: pandoc conversion failed:\n{result.stderr}", err=True)
        tmp_path.unlink(missing_ok=True)
        sys.exit(1)
    return tmp_path


def convert_to_html(source: Path) -> Path:
    """Convert a markdown file to HTML using the markdown library."""
    import markdown

    md_text = source.read_text()
    html = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
    tmp_path = Path(tempfile.mkstemp(suffix=".html")[1])
    tmp_path.write_text(html)
    return tmp_path


def upload_to_doc(service, file_id: str, local_path: Path, mime_type: str) -> None:
    """Replace Google Doc content by uploading a converted file."""
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
    service.files().update(
        fileId=file_id,
        media_body=media,
    ).execute()


@click.command()
@click.argument("document")
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["auto", "html"]),
    default="auto",
    help="Conversion format: auto (DOCX via pandoc) or html (no images)",
)
def gdoc_upload(document: str, file: str, fmt: str) -> None:
    """Replace a Google Doc's content with a local file.

    DOCUMENT can be a full Google Docs URL or a file ID.
    FILE is the local markdown file to upload.

    By default, converts to DOCX via pandoc (preserves embedded images).
    Use --format html to skip pandoc (warning: embedded images will be lost).
    """
    source = Path(file)
    file_id = extract_file_id(document)

    use_html = fmt == "html"

    if not use_html:
        if not shutil.which("pandoc"):
            click.echo(
                "Error: pandoc is required for DOCX conversion (preserves images).\n"
                "Install: pacman -S pandoc-cli\n"
                "Or use --format html (warning: embedded images will be lost).",
                err=True,
            )
            sys.exit(1)

    creds = authenticate()
    service = build_drive_service(creds)

    if use_html:
        click.echo("Converting to HTML (note: embedded images will be lost)...")
        converted = convert_to_html(source)
        mime_type = "text/html"
    else:
        click.echo("Converting to DOCX via pandoc...")
        converted = convert_to_docx(source)
        mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    try:
        click.echo("Uploading to Google Docs...")
        upload_to_doc(service, file_id, converted, mime_type)
    finally:
        converted.unlink(missing_ok=True)

    doc_url = f"https://docs.google.com/document/d/{file_id}/edit"
    click.echo(f"Done. Document updated: {doc_url}")
