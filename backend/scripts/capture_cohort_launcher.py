#!/usr/bin/env python3
"""OUTER ``-I -S`` launcher for the frozen-cohort capture producer.

The source-fence handoff hashes the scorer manifest before a fresh ``-S`` child exists.
Capture deliberately runs in its main worktree so the reviewable provenance record is
published there; its clean-tree and digest checks are producer guarantees, not release
authority.

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

from source_fence_launcher import (  # noqa: E402
    LauncherError,
    launch,
    require_isolated_launcher,
)


def main(argv: list[str] | None = None) -> int:
    # FIRST, before anything is read or hashed: a launcher started wrong has already been
    # compromised (a startup hook could rebind hashlib in THIS process).
    require_isolated_launcher()
    script_args = sys.argv[1:] if argv is None else argv
    # launch() computes the pre-exec manifest digest and hands it to the fresh -S child.
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
