# Commands Reference

All commands are available through the `devtool` CLI, installed via `uv tool install -e .`.

## AI-Powered Commands

| Command | Purpose | Key deps |
|---------|---------|----------|
| `devtool ask` | Linux command assistant — suggests shell commands from natural language | claude-agent-sdk, click |
| `devtool commit` | AI commit assistant — generates commit messages from staged changes | claude-agent-sdk, GitPython, click |
| `devtool mr-create` | Generate MR title/description from branch commits, create draft MR | claude-agent-sdk, GitPython, glab CLI |

These commands use `devtool.common.claude` for Claude Agent SDK integration with retry logic and progress UI.

## Git Utilities

| Command | Purpose | Key deps |
|---------|---------|----------|
| `devtool switch-main` | Switch to main/default branch with automatic stashing | GitPython, Rich |

## GitLab Tools

| Command | Purpose | Key deps |
|---------|---------|----------|
| `devtool merge` | Approve and merge a GitLab merge request | python-gitlab, click, Rich |
| `devtool comments` | Fetch unresolved MR discussion threads with code context | python-gitlab, Rich |
| `devtool protect` | Restore stage branch protection with safe defaults | python-gitlab |
| `devtool unprotect` | Temporarily allow force-push by removing branch protection | python-gitlab |

## Productivity

| Command | Purpose | Key deps |
|---------|---------|----------|
| `devtool weekly-status` | Jira weekly status automation — generate and publish to Confluence | atlassian-python-api, click, Rich |
| `devtool gdoc-comments` | Fetch and display Google Docs comments | google-api-python-client, click |

## Diagnostics

| Command | Purpose | Key deps |
|---------|---------|----------|
| `devtool doctor` | Run diagnostic checks for dependencies and authentication | click, Rich |
