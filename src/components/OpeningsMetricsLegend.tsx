import { useEffect, useRef, useState } from "react";

/**
 * Page-level legend for the three opening metrics. An info (ⓘ) button beside
 * the Openings Tree title toggles a popover defining Score, Coverage, and
 * Confidence once, so the per-node cards stay uncluttered. Copy is sourced from
 * docs/openingscore_final.md (the single source of truth for these meanings).
 *
 * Reuses the GameReviewStats info-button/popup interaction (click / Escape /
 * click-outside dismiss) via the shared `.info-help-*` classes.
 */
const METRICS: Array<{ term: string; desc: string }> = [
  {
    term: "Score",
    desc: "How reliably you navigate this opening's important lines (0–100, graded A–F). Higher means you stay on track through the opponent replies you've actually trained.",
  },
  {
    term: "Coverage",
    desc: "How much of the important opponent-response tree you've actually faced. Low coverage means parts of the opening haven't been tested yet.",
  },
  {
    term: "Confidence",
    desc: "How much evidence backs the score — how many times you've played and reviewed these lines, and how recently.",
  },
];

function OpeningsMetricsLegend() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span className="openings-metrics-legend" ref={ref}>
      <button
        type="button"
        className="info-help-btn openings-metrics-legend__btn"
        aria-label="What do Score, Coverage, and Confidence mean?"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="openings-metrics-legend__label">Opening Score?</span>
        <svg
          width="14"
          height="14"
          viewBox="0 0 16 16"
          aria-hidden="true"
          focusable="false"
        >
          <circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <circle cx="8" cy="4.6" r="0.95" fill="currentColor" />
          <rect x="7.2" y="6.6" width="1.6" height="5" rx="0.8" fill="currentColor" />
        </svg>
      </button>
      {open && (
        <div
          className="info-help-popup info-help-popup--wide info-help-popup--right"
          role="tooltip"
        >
          <dl className="openings-metrics-legend__list">
                            <dt>Opening Score</dt>
                            <dd>Measures how well you know the opening. Based on your play in games and drills. Graded from A-F</dd>
                            <dt>Coverage</dt>
                            <dd>How much of the opening's variations you've explored </dd>
                            <dt>Confidence</dt>
                            <dd>How confident we are in your opening score based on how many games you've played and how much coverage</dd>


          </dl>
        </div>
      )}
    </span>
  );
}

export default OpeningsMetricsLegend;
