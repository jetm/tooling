# Personal productivity scripts

## Quickstart

Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/). Scripts are executable directly:

```bash
./aca.py --help
./gitlab-mr-comments.py --help
python3 ./find_related_commits.py --help
```

## Tools

### aca.py — AI Commit Assistant

Generate commit messages and GitLab MR descriptions using Claude.

- `commit` — generate a commit message from staged changes
- `mr-desc` — generate MR title/description from branch commits, create draft MR via `glab`
- `doctor` — check dependencies and authentication

```bash
./aca.py commit
./aca.py mr-desc
```

Requires: [Claude Code CLI](https://claude.ai/download), `glab` (for `mr-desc`)

### find_related_commits.py

Find commits in a range that touched currently staged files, then optionally fixup + rebase.

```bash
git add <files>
python3 ./find_related_commits.py HEAD~10
```

### gitlab-mr-comments.py

Fetch unresolved MR discussion threads with code context (for LLM consumption).

```bash
export GITLAB_TOKEN=<token>
./gitlab-mr-comments.py --mr-url https://gitlab.com/group/project/-/merge_requests/123
```
