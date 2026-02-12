# Agent Instructions

The overall project and architecure is described in SPEC.md
When running python and related commands you need to activate the venv in backend/.venv `cd backend && source .venv/bin/activate`
If pytests fail due to temp dir, use TMPDIR=/Users/mvargas/src/ghostreplay/backend/.tmp or another valid temp path.

## Testing Guidance

- Avoid overly detailed UI/interface tests (exact layout structure, cosmetic copy, or styling-specific assertions).
- Prefer testing behavior contracts, data flow, and critical user outcomes instead of pixel-level or nav-chrome details.


## Multi-Agent Workspace Rules

This repo may be edited by multiple agents/users at the same time.

- Assume unrelated modified/untracked files are expected.
- Do not stash, reset, checkout, or revert files you did not edit for the current task.
- Do not include unrelated files in commits.
- Stage only explicit task files (path-by-path `git add/qui <file>`).
- If branch is ahead and push is requested, skip `git pull --rebase` unless explicitly told.
- If unexpected files appear, ignore them unless user asks to include them.


## Issue Tracking

This project uses **bd (beads)** for issue tracking.
Run `bd prime` for workflow context, or install hooks (`bd hooks install`) for auto-injection.

**Quick reference:**
- `bd ready` - Find unblocked work
- `bd create --id=g-<slug> --title="..." --type task --priority 2` - Create issue
- `bd close <id>` - Complete work
- `bd sync` - Sync with git (run at session end)

> **REQUIRED**: Always pass `--id=g-<slug>` when creating issues. `<slug>` must be 5–20 char lowercase kebab-case mini-summary (e.g. `g-fix-login-redirect`). Never let beads auto-generate IDs.

For full workflow details: `bd prime`

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.
Before running git workflow steps, follow `Multi-Agent Workspace Rules` above to avoid touching unrelated local changes.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
