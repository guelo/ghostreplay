import { useEffect, useRef, useState } from "react";

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
        aria-label="What do the opening metrics and move types mean?"
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
            <dd>Readiness grade from move quality, sample size, and opponent replies.</dd>
            <dt>Coverage</dt>
            <dd>
              Depth-weighted share of opening positions you’ve visited. It follows
              your chosen book routes, keeps known breadth when you leave book,
              keeps every known opponent reply, shares credit across transpositions,
              and counts off-book continuations as one branch.
            </dd>
          </dl>
          <p className="openings-metrics-legend__section-title">Move types</p>
          <ul className="openings-metrics-legend__types">
            <li>
              <span
                className="openings-metrics-legend__swatch openings-metrics-legend__swatch--book"
                aria-hidden="true"
              />
              <span>
                <strong>Book move</strong> — part of the opening book.
              </span>
            </li>
            <li>
              <span
                className="openings-metrics-legend__swatch openings-metrics-legend__swatch--off-book"
                aria-hidden="true"
              />
              <span>
                <strong>Off book</strong> — a move from your own games, not in
                the book. Flagged by a violet tag on the card (its connector
                stays the standard book/observed colour).
              </span>
            </li>
            <li>
              <span
                className="openings-metrics-legend__swatch openings-metrics-legend__swatch--transposition"
                aria-hidden="true"
              />
              <span>
                <strong>Transposition</strong> — in the opening book, but reached
                through a different move order.
              </span>
            </li>
            <li>
              <span
                className="openings-metrics-legend__swatch openings-metrics-legend__swatch--selected"
                aria-hidden="true"
              />
              <span>
                <strong>Board move</strong> — a move you explored on the board,
                shown while it's part of the current line.
              </span>
            </li>
          </ul>
        </div>
      )}
    </span>
  );
}

export default OpeningsMetricsLegend;
