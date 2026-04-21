---
name: plans_in_beads
description: Always update the bead's design field with plan content before exiting plan mode
type: feedback
---

When creating implementation plans, update the bead's design field (`bd update <id> --design "..."`) with the plan content before requesting approval via ExitPlanMode. The user wants plans persisted in beads, not just in ephemeral plan files.

**Why:** Plans in beads survive across sessions and are visible via `bd show`. The plan file is ephemeral.
**How to apply:** After writing/updating the plan file, always sync it to the bead's design field before calling ExitPlanMode.
