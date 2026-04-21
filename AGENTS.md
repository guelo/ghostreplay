# Agent Instructions

The initial project and architecture is described in SPEC.md but we are now in a post-SPEC environment, changes have been made that are beyond the spec.
The spec will become more and more outdated over time. It might still be useful to get an idea of the overall project.

When running python and related commands you need to activate the venv in backend/.venv `cd backend && source .venv/bin/activate`
If pytests fail due to temp dir, use TMPDIR=/Users/mvargas/src/ghostreplay/backend/.tmp or another valid temp path.

## Frontend Architecture
- React 19 + Vite + TypeScript
- Main game component: `src/components/ChessGame.tsx` (~1000 lines)
- Styles: `src/App.css` (no CSS modules)
- Toast pattern: absolute-positioned in `.chessboard-board-area`, z-index 8, slide-up animation via `blunder-toast-in` keyframes
- Blunder review flow: `blunderReviewId` set in `applyGhostMove` when `target_blunder_id` received from API

## Design Preferences
- [Zustand store design](docs/memory/feedback_zustand_store_design.md) — narrow stores, derive cheap values, keep Chess + effects outside

## Feedback
- [No commit without review](docs/memory/feedback_no_commit_without_review.md) — do not commit until user has reviewed changes
- [Thorough plans](docs/memory/feedback_thorough_plans.md) — plans must trace data flows, layout math, sizing constraints, and test coverage precisely
- [Plans in beads](docs/memory/feedback_plans_in_beads.md) — always update bead design field with plan content before exiting plan mode

## Testing Guidance
- Avoid overly detailed UI/interface tests (exact layout structure, cosmetic copy, or styling-specific assertions).
- Prefer testing behavior contracts, data flow, and critical user outcomes instead of pixel-level or nav-chrome details.

## Multi-Agent Workspace Rules

This repo may be edited by multiple agents/users at the same time.

- Assume unrelated modified/untracked files are expected.
- NEVER stash, reset, checkout, or revert files you didn't edit
- Do not include unrelated files in commits.
- Stage only files for your task (path-by-path `git add`)
- If branch is ahead and push is requested, skip `git pull --rebase` unless explicitly told.
- If unexpected files appear, ignore them unless user asks to include them.



<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd create --id=g-<slug> --title="..." --type task --priority 2` # Create issue
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
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
<!-- END BEADS INTEGRATION -->

**CRITICAL**: Use the `bd` CLI tool exclusively. Never read or modify files in the `.beads/` directory directly.

 **REQUIRED**: Always pass `--id=g-<slug>` when creating issues. `<slug>` must be 5–20 char lowercase kebab-case mini-summary (e.g. `g-fix-login-redirect`). Never let beads auto-generate IDs.


