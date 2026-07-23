#!/usr/bin/env bash
# The capture-cohort entrypoint (g-p4ih-capture), mirroring release_calibration.sh.
#
# This wrapper exists for one reason: the launcher's flags cannot be its own responsibility.
# `site.py` runs every .pth in site-packages and imports sitecustomize BEFORE the launcher's
# first line — early enough to replace hashlib in the process that computes the digest — so
# the flags have to be right at exec time or not at all, which makes them the caller's job.
#
#   -S  no site initialisation: no .pth, no sitecustomize.
#   -I  isolated: implies -E (PYTHONPATH cannot shadow the stdlib the launcher imports,
#       PYTHONHOME cannot relocate it) and -s (no user site).
#
# UNLIKE release_calibration.sh, capture runs the child in the MAIN worktree (no throwaway
# exclusive checkout), so the reviewable cohort_provenance.json diff lands where a human
# commits it. The launcher still computes the pre-exec source digest over the main worktree
# and hands it to the -S child, and the child refuses a dirty derivation tree.
#
# The interpreter matters as much as the flags: the launcher execs the scorer with its OWN
# sys.executable and derives the child's dependency paths from that interpreter's venv, so
# this must be the python whose environment has the scorer's deps installed. Defaulting to
# `python3` through PATH would silently capture against whatever versions the system carries,
# so we default to the repo venv and FAIL CLOSED (exit 2) rather than fall back to PATH.
# GHOSTREPLAY_PYTHON stays as the EXPLICIT override.
#
# The release-guard user is supplied ONLY through GHOSTREPLAY_RELEASE_GUARD_USER (never a CLI
# argument: a production user id must not enter shell history or every process listing).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
default_python="$(cd "${here}/.." && pwd)/.venv/bin/python"
python="${GHOSTREPLAY_PYTHON:-${default_python}}"

if [[ ! -x "${python}" ]]; then
  echo "capture_cohort: no usable interpreter at '${python}'." >&2
  if [[ -z "${GHOSTREPLAY_PYTHON:-}" ]]; then
    echo "  Expected the repo venv at ${default_python}. Create it, or set GHOSTREPLAY_PYTHON" >&2
    echo "  to the interpreter whose environment has the scorer's dependencies." >&2
    echo "  Refusing to fall back to PATH: that silently captures against system versions." >&2
  fi
  exit 2
fi

exec "${python}" -I -S "${here}/capture_cohort_launcher.py" "$@"
