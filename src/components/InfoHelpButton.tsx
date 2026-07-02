import { useEffect, useRef, useState, type ReactNode } from "react";

type InfoHelpButtonProps = {
  /** Accessible label for the trigger button. */
  ariaLabel: string;
  /** Popup contents shown while open. */
  children: ReactNode;
  /** Extra class(es) for the positioning anchor `<span>`. */
  className?: string;
  /** Extra class(es) for the popup (e.g. `info-help-popup--wide`). */
  popupClassName?: string;
};

/**
 * Shared info (ⓘ) button + popover: click to toggle, dismiss on outside-click
 * or Escape. Wraps the `.info-help-btn` / `.info-help-popup` styles so callers
 * only supply the popup content. See also OpeningsMetricsLegend and
 * GameReviewStats, which predate this component.
 */
function InfoHelpButton({
  ariaLabel,
  children,
  className,
  popupClassName,
}: InfoHelpButtonProps) {
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
    <span className={`info-help${className ? ` ${className}` : ""}`} ref={ref}>
      <button
        type="button"
        className="info-help-btn"
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
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
          className={`info-help-popup${popupClassName ? ` ${popupClassName}` : ""}`}
          role="tooltip"
        >
          {children}
        </div>
      )}
    </span>
  );
}

export default InfoHelpButton;
