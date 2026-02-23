# devtool

Developer workflow toolkit — AI-powered commit messages, Linux command suggestions, Jira status automation, GitLab merge management, and more.

## Quickstart

Requires Python >= 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
# Install as a CLI tool
uv tool install -e .

# Verify installation
devtool --help
devtool doctor
```

## Commands

### AI-Powered

**`devtool commit`** — Generate commit messages from staged changes using Claude. Automatically compresses large diffs and runs pre-commit hooks before committing.

```bash
devtool commit              # generate and review commit message
devtool commit -y           # auto-confirm
devtool commit -t           # title-only mode (single line, uses haiku)
devtool commit --no-compress  # disable diff compression
```

**`devtool ask`** — Get Linux command suggestions from Claude and execute them interactively. Detects destructive commands and prompts for confirmation.

```bash
devtool ask "show disk usage"
devtool ask "list running processes sorted by memory"
```

**`devtool mr-create`** — Generate MR title and description from branch commits, create draft MR via `glab`.

```bash
devtool mr-create
devtool mr-create --base main
```

### GitLab

**`devtool merge`** — Approve and merge a GitLab merge request with pipeline checks.

```bash
devtool merge https://gitlab.com/group/project/-/merge_requests/123
```

**`devtool comments`** — Fetch unresolved MR discussion threads with code context.

```bash
devtool comments https://gitlab.com/group/project/-/merge_requests/123
```

**`devtool protect`** / **`devtool unprotect`** — Manage GitLab branch protection (temporarily allow force-push, then restore).

```bash
devtool unprotect --project-url https://gitlab.com/group/project
devtool protect --project-url https://gitlab.com/group/project
```

### Git

**`devtool switch-main`** — Switch to main/default branch with automatic stashing. Auto-detects the main branch and caches it.

```bash
devtool switch-main
```

### Productivity

**`devtool weekly-status`** — Generate Jira weekly status reports and publish to Confluence.

```bash
devtool weekly-status             # current week
devtool weekly-status --week 4    # specific week
devtool weekly-status diagnose    # validate configuration
```

**`devtool gdoc-comments`** — Fetch and display Google Docs comments.

```bash
devtool gdoc-comments <document-url-or-id>
```

### Diagnostics

**`devtool doctor`** — Check dependencies, authentication, and configuration.

```bash
devtool doctor
devtool doctor --full    # include live API test
```

## Architecture

The project is structured as a Python package under `src/devtool/`:

```
src/devtool/
├── cli.py              # Click group, registers all subcommands
├── _deps.py            # Lazy dependency imports
├── common/             # Shared infrastructure
│   ├── claude.py       # Claude Agent SDK integration
│   ├── config.py       # Configuration loading (~/.config/aca/config.toml)
│   ├── console.py      # Rich console, logging, dependency checks
│   ├── errors.py       # Error hierarchy, retry logic
│   └── git.py          # Editor integration, text processing
├── ask/                # devtool ask
├── commit/             # devtool commit (includes diff compression)
├── doctor/             # devtool doctor
├── gdoc/               # devtool gdoc-comments
├── git/                # devtool switch-main
├── gitlab/             # devtool merge, comments, protect, unprotect
├── mr_create/            # devtool mr-create
└── weekly_status/      # devtool weekly-status
```

AI-powered commands (`ask`, `commit`, `mr-create`) use the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) which wraps the Claude Code CLI. Authentication is handled by the CLI — no API keys needed.

## Configuration

All AI commands share configuration via `~/.config/aca/config.toml` and environment variables (prefixed `ACA_`). Run `devtool doctor` to validate your setup.

Requirements: [Claude Code CLI](https://claude.ai/download) for AI commands, `glab` for GitLab commands, Jira/Confluence credentials for `weekly-status`.
