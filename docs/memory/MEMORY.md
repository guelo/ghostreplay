# Ghost Replay Project Memory

## Multi-Agent Workspace Rules
- NEVER stash, reset, checkout, or revert files you didn't edit
- Stage only files for your task (path-by-path `git add`)
- Commit beads changes separately before `git pull --rebase`
- Unrelated unstaged files are expected — ignore them
- See AGENTS.md for full rules

## Frontend Architecture
- React 19 + Vite + TypeScript
- Main game component: `src/components/ChessGame.tsx` (~1200 lines)
- Styles: `src/App.css` (no CSS modules)
- Toast pattern: absolute-positioned in `.chessboard-board-area`, z-index 8, slide-up animation via `blunder-toast-in` keyframes
- Blunder review flow: `blunderReviewId` set in `applyGhostMove` when `target_blunder_id` received from API

## Design Preferences
- [Zustand store design](feedback_zustand_store_design.md) — narrow stores, derive cheap values, keep Chess + effects outside

## Feedback
- [No commit without review](feedback_no_commit_without_review.md) — do not commit until user has reviewed changes
- [Thorough plans](feedback_thorough_plans.md) — plans must trace data flows, layout math, sizing constraints, and test coverage precisely
- [Plans in beads](feedback_plans_in_beads.md) — always update bead design field with plan content before exiting plan mode
- [Full plans in beads](feedback_plans_in_beads_full.md) — bead design field must contain the full plan, not a condensed summary

## Beads Workflow
- Use `bd close --force` when closing issues with open blockers (if UI-only work is complete)
- Always `bd sync` before and after commits
