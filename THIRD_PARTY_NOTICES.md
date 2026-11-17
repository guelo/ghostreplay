# Third-party notices

## Maia3 (code and weights): AGPL-3.0

The backend's opponent engine is Maia3, by the University of Toronto
Computational Social Science Lab (CSSLab).

| Component | Source | Pinned at |
|-----------|--------|-----------|
| Code (`maia3` 0.1.0) | https://github.com/CSSLab/maia3 | commit `1e13597c42d4858b7cfd7cfdae01e297263364b2` |
| Weights `maia3-23m.pt` (default) | https://huggingface.co/UofTCSSLab/Maia3-23M | revision `51a0145a8178046f7de23119160b136672deeb2b` |
| Weights `maia3-5m.pt` (fallback) | https://huggingface.co/UofTCSSLab/Maia3-5M | revision `b6559de2398d7140b985f28fd2c19fb5e47ddabe` |

The pins, with each checkpoint's size and sha256, are in
`backend/app/maia3_pins.py`. The code repository is licensed under the GNU Affero
General Public License v3.0. The Hugging Face repositories state no separate
license and point to the code repository, so the weights are treated as AGPL-3.0
too.

How this project uses it (decision 1 of g-maia3-local-engine, 2026-11-14):

- The code and the weights are used **unmodified**. The code is installed from
  the upstream archive at the pinned commit (`backend/requirements-maia3.txt`),
  and the weights are downloaded at the pinned revisions and checked against
  their sha256 (`backend/scripts/prepare_maia3_engine.py`).
- Maia3 runs **only as a separate process** (`python -m maia3.uci`), which the
  API drives over the UCI protocol on stdin/stdout pipes, the way a chess GUI
  drives an engine (`backend/app/maia3_client.py`). The API process never
  imports `maia3` or `torch`; a test checks that importing the API loads
  neither.
- Importing Maia3 into the API process, or modifying it, would reopen this
  decision.

The full license text is in the upstream repository's `LICENSE` file at the
pinned commit:
https://github.com/CSSLab/maia3/blob/1e13597c42d4858b7cfd7cfdae01e297263364b2/LICENSE
