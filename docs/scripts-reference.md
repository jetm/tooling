# Scripts Reference

All scripts use [PEP 723](https://peps.python.org/pep-0723/) inline metadata for dependencies and are run with `uv run`.

## AI-Powered Scripts

| Script | Binary name | Purpose | Key deps |
|--------|------------|---------|----------|
| `aca.py` | `aca` | AI commit assistant — generates commit messages from staged changes | claude-agent-sdk, GitPython, click |
| `lx.py` | `lx` | Linux command assistant — suggests shell commands from natural language | claude-agent-sdk, click |

Both import shared utilities from `common_utils.py` for Claude SDK integration, retry logic, and Rich console output.

## Git Utilities

| Script | Binary name | Purpose | Key deps |
|--------|------------|---------|----------|
| `find-related-commits.py` | `find-related-commits` | Find commits where staged files were modified in a given range | GitPython |
| `git-switch-main.py` | `git-switch-main` | Switch to main/default branch with automatic stashing | GitPython, Rich |

## GitLab Tools

| Script | Binary name | Purpose | Key deps |
|--------|------------|---------|----------|
| `gitlab-lean.py` | `gitlab-lean` | MR approval, merge, and force-push management | python-gitlab, click, Rich |
| `gitlab-mr-comments.py` | `gitlab-mr-comments` | Fetch and display MR comments | python-gitlab, Rich |

## Productivity

| Script | Binary name | Purpose | Key deps |
|--------|------------|---------|----------|
| `weekly_status.py` | `weekly-status` | Jira weekly status automation | atlassian-python-api, click, Rich |
| `gdoc-comments.py` | `gdoc-comments` | Fetch Google Docs comments | google-api-python-client, click |

## Maintenance Scripts

| Script | Purpose | Key deps |
|--------|---------|----------|
| `update-claude-md.py` | Auto-update CLAUDE.md with current project state | (stdlib only) |
| `validate-python-version.py` | Validate requires-python in uv script blocks | (stdlib only) |

## Chezmoi Integration (`bin/`)

Each script has a corresponding chezmoi symlink template in `bin/`:

```
bin/
├── symlink_aca.tmpl              → aca.py
├── symlink_find-related-commits.tmpl → find-related-commits.py
├── symlink_gdoc-comments.tmpl    → gdoc-comments.py
├── symlink_gitlab-lean.tmpl      → gitlab-lean.py
├── symlink_gitlab-mr-comments.tmpl → gitlab-mr-comments.py
├── symlink_git-switch-main.tmpl  → git-switch-main.py
├── symlink_git-undo.tmpl         → git-undo.sh
├── symlink_lx.tmpl               → lx.py
├── symlink_update-claude-md.tmpl → update-claude-md.py
├── symlink_weekly-status.tmpl    → weekly_status.py
└── executable_get-biscuit.tmpl   → encrypted biscuit token script
```

When adding a new script, create a corresponding `bin/symlink_<name>.tmpl` so chezmoi installs it to `~/.local/bin/`.
