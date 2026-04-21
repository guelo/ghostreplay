---
name: Plans must be implementation-precise
description: User expects plans to handle edge cases in prop wiring, layout math, and test coverage before approval
type: feedback
---

Plans must be precise enough to implement without ambiguity. Specifically:

- **Prop wiring**: Don't assume a value is available — trace the actual data path. E.g., `currentIndex` can be null for "latest" view; use the derived memo instead.
- **Layout math**: When positioning elements dynamically, account for fixed-size siblings. A naive percentage on a container with fixed headers/footers will misalign with the target area.
- **Width/sizing**: When adding flex children to a size-constrained container, state explicitly whether the constraint applies to the whole control or just the content area.
- **Test coverage**: Every new code path must appear in the test plan. If a component has no tests, call out that a new test file is needed. Don't leave the new logic uncovered just because existing tests don't cover the file.

**Why:** User reviewed plans iteratively and caught: wrong prop source (evals[currentIndex] vs currentEvalCp), underspecified positioning (percentage on wrong container), missing width impact analysis, and uncovered test paths.

**How to apply:** Before finalizing a plan, trace each data flow end-to-end and verify every new branch has a corresponding test entry.
