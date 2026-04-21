¡¡---
name: Full plans in beads
description: Bead design field should contain the full plan, not a condensed summary
type: feedback
---

When writing plans to the bead design field, use the full plan content — don't condense or summarize it. The bead design field is the canonical location for the plan.

**Why:** User noticed the bead had a shorter version than the plan file and flagged it. The bead should be the single source of truth.

**How to apply:** When updating `bd update <id> --design`, pass the full plan file content (e.g. `cat` the plan file into the command). Don't manually write a shorter version.
