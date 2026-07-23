#!/usr/bin/env python3
"""OUTER -I -S launcher for the capture-cohort producer (g-p4ih-capture).

Capture manufactures the frozen overlay the whole release path later treats as ground
truth, so a reproducible-source guarantee matters MORE on the producer side than on the
consumer side, not less. This launcher gives capture the SAME two-process startup isolation
the release path already proves — ``python -I -S`` launcher -> ``-S`` child with a pre-exec
source digest computed BEFORE the child interpreter exists (closing the compile window) —
by REUSING ``release_calibration_launcher.launch`` verbatim, not forking a second copy.

The ONE deliberate difference from the release launcher: capture does NOT relocate to a
throwaway exclusive worktree. It runs the child in the MAIN worktree so the reviewable
``cohort_provenance.json`` diff lands where a human commits it (``COHORT_PROVENANCE_PATH``
resolves relative to the executing checkout — a launcher-hosted capture would write it into
a directory destroyed on exit). The child's clean-tree refusal over ``SCORER_SOURCE_FILES``
is capture's (honestly weaker) substitute for the launcher's read-only exclusive checkout.

USAGE
-----
    backend/scripts/capture_cohort.sh --output PATH [--require-quiescent-epoch]

or, equivalently and explicitly::

    python -I -S backend/scripts/capture_cohort_launcher.py --output PATH [--require-quiescent-epoch]

``--output`` MUST BE ABSOLUTE. This launcher forwards script arguments verbatim, and
``launch()`` starts the child with ``cwd=<tree>/backend`` — so a relative path would resolve
in the child against a directory the operator never chose. Rather than guess which of the
two directories was meant, the child refuses a relative ``--output`` outright
(``_refuse_repo_interior_output``).

``-I -S`` IS NOT OPTIONAL (see ``require_isolated_launcher``): without it this process is
contaminated before it hashes anything. Use the interpreter whose environment has the
scorer's dependencies — the child inherits ``sys.executable`` and derives its deps from that
interpreter's venv. The release-guard user is supplied ONLY through
``GHOSTREPLAY_RELEASE_GUARD_USER`` (never a CLI argument), and is forwarded to the child
through the inherited environment.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Sibling import: both launchers live in backend/scripts/. Under -I -S sys.path[0] is this
# directory, but make it explicit so the import cannot depend on invocation form.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from release_calibration_launcher import (  # noqa: E402
    LauncherError,
    launch,
    require_isolated_launcher,
)


def main(argv: list[str] | None = None) -> int:
    # FIRST, before anything is read or hashed: a launcher started wrong has already been
    # compromised (a startup hook could rebind hashlib in THIS process).
    require_isolated_launcher()
    script_args = sys.argv[1:] if argv is None else argv
    # The MAIN worktree (origin checkout) — NOT an exclusive_checkout. launch() computes
    # manifest_digest(main_worktree) and exports it via child_env BEFORE exec'ing the -S
    # child, so the child inherits the pre-exec digest as _LAUNCHER_SCORER_DIGEST.
    main_worktree = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="ghostreplay-capture-pycache-") as pycache:
        return launch(
            main_worktree,
            ["capture-cohort", *script_args],
            pycache_dir=Path(pycache),
        )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LauncherError as exc:
        print(f"[capture-launcher] refusing to run: {exc}", file=sys.stderr)
        sys.exit(2)
