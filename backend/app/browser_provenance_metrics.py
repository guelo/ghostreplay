"""Fleet-adoption metric for browser-game-v2 provenance (g-mk1d §2.4.1).

This module owns the ONE tested implementation of the browser-game-v1 retirement
criterion's math. The criterion (g-mk1d §0.6, executed by ``g-bgv1-cutover``) is:

    retirement becomes eligible once the fraction of DISTINCT sessions whose
    latest FINAL coalesced run reports ``session_provenance == "v2"`` clears a
    threshold over a trailing window.

Two grain mistakes it deliberately avoids:

* NOT row-weighted. The ``provenance_valid/absent/malformed`` counts on the same
  ``analysis_cache_write`` log line are per-ROW, so a handful of long legacy-client
  games would swamp the signal. Those counts are the operational health check
  ("are malformed rows appearing at all?"), never the adoption metric.
* NOT per-run. One session emits MANY coalesced scheduler runs over its lifetime
  (each incremental upload plus the final one can trigger its own run), so counting
  runs would re-weight adoption by how often a session happened to upload.

So the rollup collapses to exactly one verdict per ``session_id``: that session's
latest FINAL run. A session with no final run in the window is still in progress
and is excluded rather than counted as non-adopted.

The INPUT (:class:`RunVerdict` records) is obtained by scraping the structured
``analysis_cache_write`` summary lines — the same log-scrape path as the g-dckw
latency metric. That adapter is thin and separable on purpose: the rollup MATH
lives here and is unit-testable without a log pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Per-coalesced-run provenance verdicts stamped on the analysis_cache_write log.
PROVENANCE_V2 = "v2"  # >=1 move validated as browser-game-v2, none malformed
PROVENANCE_LEGACY = "legacy"  # every browser-eligible move carried no provenance
PROVENANCE_MIXED_MALFORMED = "mixed_malformed"  # at least one malformed claim
# No browser-eligible move at all (e.g. an upload of only synthetic-terminal or
# eval-less moves). Carries NO evidence either way, so the rollup excludes it
# rather than scoring it as non-adopted.
PROVENANCE_NONE = "none"


def session_provenance_verdict(*, valid: int, absent: int, malformed: int) -> str:
    """Collapse one coalesced run's per-row counts into its single verdict.

    Malformed dominates: a run that produced ANY malformed claim is reported as
    ``mixed_malformed`` even if other rows validated, so a client bug can never
    hide inside an otherwise-adopted session.
    """
    if malformed > 0:
        return PROVENANCE_MIXED_MALFORMED
    if valid > 0:
        return PROVENANCE_V2
    if absent > 0:
        return PROVENANCE_LEGACY
    return PROVENANCE_NONE


@dataclass(frozen=True)
class RunVerdict:
    """One coalesced ``analysis_cache_write`` run, as scraped from its log line."""

    session_id: str
    # Scrape this from the log line's ``session_final`` field, NOT ``final``.
    # ``final`` is g-dckw's latency cohort and tracks ``run_opportunity``, which
    # the revert upload and every pre-g-y90g client also set True on non-final
    # uploads. ``session_final`` tracks ``terminal_action``, the only signal that
    # actually means "this session ended".
    final: bool
    session_provenance: str
    ts: float  # any monotonically comparable timestamp (log time)


@dataclass(frozen=True)
class AdoptionStats:
    sessions_considered: int
    sessions_v2: int
    # Distinct sessions that emitted runs in the window but never a FINAL one.
    # Reported rather than silently dropped because the exclusion is not neutral:
    # a client too old to send terminal_action is also too old to send provenance,
    # so those sessions are both invisible here AND certainly non-adopted. If this
    # is not small relative to sessions_considered, ``fraction`` OVERSTATES
    # adoption and must not be used to gate browser-game-v1 retirement.
    sessions_without_final: int = 0

    @property
    def fraction(self) -> float:
        """v2 share of considered sessions; ``0.0`` when nothing was considered."""
        if self.sessions_considered == 0:
            return 0.0
        return self.sessions_v2 / self.sessions_considered


def session_v2_adoption(runs: Iterable[RunVerdict]) -> AdoptionStats:
    """Distinct-session browser-game-v2 adoption over the supplied runs.

    Drops non-final runs, groups the rest by ``session_id``, keeps the max-``ts``
    verdict per session, and reports the v2 share. Sessions whose latest final run
    carries no browser-eligible move (``none``) are excluded from the denominator:
    they are evidence of nothing, and counting them would understate adoption.

    Sessions with NO final run are excluded too, but COUNTED into
    ``sessions_without_final`` — see that field for why the exclusion is a live
    threat to the metric's validity rather than a neutral filter.
    """
    latest: dict[str, RunVerdict] = {}
    seen_sessions: set[str] = set()
    for run in runs:
        seen_sessions.add(run.session_id)
        if not run.final:
            continue
        current = latest.get(run.session_id)
        if current is None or run.ts >= current.ts:
            latest[run.session_id] = run

    considered = [
        run for run in latest.values() if run.session_provenance != PROVENANCE_NONE
    ]
    return AdoptionStats(
        sessions_considered=len(considered),
        sessions_v2=sum(
            1 for run in considered if run.session_provenance == PROVENANCE_V2
        ),
        sessions_without_final=len(seen_sessions - latest.keys()),
    )
