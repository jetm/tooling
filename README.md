# Personal productivity scripts

## Quickstart

Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/). Scripts are executable directly:

```bash
./aca.py --help
./gitlab-mr-comments.py --help
python3 ./find_related_commits.py --help
./git-switch-main.py
./git-undo --help
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

### git-undo

Undo git commits with soft or hard reset, with safety checks to prevent data loss.

```bash
./git-undo           # soft reset last commit, keep changes staged
./git-undo 3         # soft reset last 3 commits
./git-undo --soft 2  # soft reset, unstage changes
./git-undo --hard 1  # hard reset, discard changes - prompts for confirmation
```

Note: Prevents hard reset of all commits to avoid repository corruption.

### git-switch-main.py

Intelligently switch to the main/default branch (stage/main/master) with automatic stashing.

**Features:**
- Auto-detects main branch via origin/HEAD or common names
- Caches detected branch in git config (`branch-switch.name`)
- Automatically stashes uncommitted changes (including untracked files)
- Restores stash after successful branch switch
- Creates local tracking branch if needed

```bash
./git-switch-main.py
```

Manual override: `git config branch-switch.name <branch>`

Requires: Python >= 3.12, uv, GitPython, Rich

## Skipping Pre-commit Hooks

The `aca.py commit` command runs pre-commit hooks on staged files before committing. This ensures code passes validation before the commit is created.

To bypass pre-commit hooks temporarily, set the `SKIP_PRECOMMIT` environment variable:

```bash
SKIP_PRECOMMIT=1 ./aca.py commit
```

**Note:** This is different from `git commit --no-verify`, which skips git hooks. The `SKIP_PRECOMMIT` variable specifically bypasses the pre-commit tool validation that `aca.py` runs.

**Use cases:**
- CI environments where hooks are run separately
- Emergency fixes when hooks are temporarily broken
- When hooks are known to pass but you want to skip re-running them

**Warning:** Use sparingly to maintain code quality. Pre-commit hooks help catch issues early.
