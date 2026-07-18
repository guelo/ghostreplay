#!/usr/bin/env bash
# The release calibration entrypoint (g-p4ih-srcfence).
#
# This wrapper exists for one reason: the launcher's flags cannot be its own responsibility.
# `site.py` runs every .pth in site-packages and imports sitecustomize BEFORE the launcher's
# first line — early enough to replace hashlib in the process that computes the digest — so
# by the time the launcher could re-exec itself under -I -S, whatever was going to happen has
# already happened. The flags have to be right at exec time or not at all, and that makes
# them the caller's job. The launcher fails closed if this wrapper is bypassed and they are
# missing; it does not silently repair them.
#
#   -S  no site initialisation: no .pth, no sitecustomize.
#   -I  isolated: implies -E (PYTHONPATH cannot shadow the stdlib the launcher imports,
#       PYTHONHOME cannot relocate it) and -s (no user site).
#
# The interpreter matters as much as the flags. The launcher execs the scorer with its OWN
# sys.executable and derives the child's dependency paths from that interpreter's venv, so
# this must be the python whose environment has the scorer's deps installed.
#
# DEFAULTING TO `python3` WAS A BUG, not a convenience. In an unactivated repo shell that
# resolves through PATH to the system interpreter — on this machine /usr/local/bin/python3,
# Python 3.10, while backend/.venv is 3.12. The launcher accepts a non-venv interpreter (a
# CI image with deps installed globally is legitimate), so nothing downstream would have
# objected: the release would simply have scored against whatever versions the system
# happened to carry. A release must not depend on whether someone remembered to activate.
#
# So: default to the repo's venv, and fail loudly if it is not there rather than falling
# back to PATH. GHOSTREPLAY_PYTHON stays as the EXPLICIT override for deployments whose
# interpreter lives elsewhere — explicit being the point.
#
# This wrapper is NOT a trust boundary. GHOSTREPLAY_PYTHON and a hostile PATH are still
# whatever the caller makes them; it removes the ambient footgun (bare launcher, wrong
# python), which is the realistic failure, not the adversarial one.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
default_python="$(cd "${here}/.." && pwd)/.venv/bin/python"
python="${GHOSTREPLAY_PYTHON:-${default_python}}"

if [[ ! -x "${python}" ]]; then
  echo "release_calibration: no usable interpreter at '${python}'." >&2
  if [[ -z "${GHOSTREPLAY_PYTHON:-}" ]]; then
    echo "  Expected the repo venv at ${default_python}. Create it, or set GHOSTREPLAY_PYTHON" >&2
    echo "  to the interpreter whose environment has the scorer's dependencies." >&2
    echo "  Refusing to fall back to PATH: that silently scores against system versions." >&2
  fi
  exit 2
fi

exec "${python}" -I -S "${here}/release_calibration_launcher.py" "$@"
