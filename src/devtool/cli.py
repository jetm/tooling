"""Root CLI entry point for devtool."""

import click

from devtool import __version__
from devtool.ask.command import ask
from devtool.commit.command import commit
from devtool.doctor.command import doctor
from devtool.gdoc.comments import gdoc_comments
from devtool.gdoc.resolve import gdoc_resolve
from devtool.gdoc.upload import gdoc_upload
from devtool.git.switch_main import switch_main
from devtool.gitlab.comments import comments
from devtool.gitlab.merge import merge
from devtool.gitlab.protect import protect, unprotect
from devtool.jira.command import jira
from devtool.mr_create.command import mr_create
from devtool.weekly_status.command import weekly_status


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Developer workflow toolkit."""


cli.add_command(ask)
cli.add_command(comments)
cli.add_command(commit)
cli.add_command(doctor)
cli.add_command(gdoc_comments, "gdoc-comments")
cli.add_command(gdoc_resolve, "gdoc-resolve")
cli.add_command(gdoc_upload, "gdoc-upload")
cli.add_command(jira)
cli.add_command(merge)
cli.add_command(mr_create, "mr-create")
cli.add_command(protect)
cli.add_command(switch_main, "switch-main")
cli.add_command(unprotect)
cli.add_command(weekly_status, "weekly-status")
