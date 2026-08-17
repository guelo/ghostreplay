import { useEffect, useRef, useState } from 'react';
import { CLASS_KEYS, type SideStats, type StatSelection, type ClassKey } from '../utils/gameStats';
import { accuracyColor, acplColor } from '../utils/statColor';
import { CLASSIFICATION_ICON } from './MoveRow.helpers';
import './GameReviewStats.css';

const ACCURACY_INFO =
  'How closely your moves matched the engine\'s best moves across the game.\n\n 100% means perfect play. Lower scores reflect how much each move gave up.'

const CLASSIFICATION_LABELS: Record<
  ClassKey,
  { accessible: string; full: string; compact: string }
> = {
  blunder: { accessible: 'Blunders', full: 'Blunders', compact: 'Blund.' },
  mistake: { accessible: 'Mistakes', full: 'Mistakes', compact: 'Mist.' },
  inaccuracy: { accessible: 'Inaccuracies', full: 'Inaccur.', compact: 'Inacc.' },
};

interface GameReviewStatsProps {
  sideStats: { player: SideStats; opponent: SideStats };
  activeStat: StatSelection;
  pinnedStat: StatSelection;
  totalMoves: number;
  accuracy: number | null;
  /** True while analysis is still processing and accuracy is not yet available. */
  accuracyPending?: boolean;
  onStatHover: (sel: StatSelection) => void;
  onStatClick: (sel: StatSelection) => void;
}

function GameReviewStats({ sideStats, activeStat, pinnedStat, totalMoves, accuracy, accuracyPending = false, onStatHover, onStatClick }: GameReviewStatsProps) {
  const [infoOpen, setInfoOpen] = useState(false);
  const infoRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!infoOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (infoRef.current && !infoRef.current.contains(e.target as Node)) {
        setInfoOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setInfoOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [infoOpen]);

  return (
    <div className="history-stats-pane">
      <div className="history-stats-pane__grid">
        {/* Header row — You/Ghost headers highlight all of that side's dots */}
        <div className="history-stats-pane__header" />
        {(['player', 'opponent'] as const).map((side) => {
          const headerSel = { side, cls: 'all' as const };
          const isActive = activeStat?.side === side && activeStat?.cls === 'all';
          const isPressed = pinnedStat?.side === side && pinnedStat?.cls === 'all';
          const text = side === 'player' ? 'You' : 'Ghost';
          const ariaLabel =
            side === 'player'
              ? 'All of your blunders, mistakes, and inaccuracies'
              : "All of Ghost's blunders, mistakes, and inaccuracies";
          return (
            <button
              key={`header-${side}`}
              type="button"
              aria-label={ariaLabel}
              aria-pressed={isPressed}
              className={`history-stats-pane__header history-stats-pane__header--interactive${isActive ? ' history-stats-pane__header--active' : ''}`}
              onMouseEnter={() => onStatHover(headerSel)}
              onMouseLeave={() => onStatHover(null)}
              onClick={() => onStatClick(headerSel)}
            >
              {text}
            </button>
          );
        })}

        {/* Classification rows */}
        {CLASS_KEYS.map((cls: ClassKey) => {
          const labels = CLASSIFICATION_LABELS[cls];
          const iconInfo = CLASSIFICATION_ICON[cls];
          const labelSel = { side: 'player' as const, cls };
          const isLabelActive = activeStat?.cls === cls;
          const isLabelPressed = pinnedStat?.side === 'player' && pinnedStat?.cls === cls;
          return [
            <button
              key={`${cls}-label`}
              type="button"
              aria-label={`Your ${labels.accessible}`}
              aria-pressed={isLabelPressed}
              className={`history-stats-pane__label history-stats-pane__label--${cls} history-stats-pane__label--interactive${isLabelActive ? ' history-stats-pane__label--active' : ''}`}
              onMouseEnter={() => onStatHover(labelSel)}
              onMouseLeave={() => onStatHover(null)}
              onClick={() => onStatClick(labelSel)}
            >
              {iconInfo && (
                <span
                  className={`history-stats-pane__label-icon move-icon move-icon--${cls}`}
                  aria-hidden="true"
                >
                  {iconInfo.icon}
                </span>
              )}
              <span className="history-stats-pane__label-copy history-stats-pane__label-copy--full">
                {labels.full}
              </span>
              <span
                className="history-stats-pane__label-copy history-stats-pane__label-copy--compact"
                aria-hidden="true"
              >
                {labels.compact}
              </span>
            </button>,
            ...(['player', 'opponent'] as const).map((side) => {
              const sel = { side, cls };
              const isActive = activeStat?.side === side && activeStat?.cls === cls;
              const isPressed = pinnedStat?.side === side && pinnedStat?.cls === cls;
              const sideLabel = side === 'player' ? 'Your' : 'Ghost';
              return (
                <button
                  key={`${cls}-${side}`}
                  type="button"
                  aria-label={`${sideLabel} ${labels.accessible}: ${sideStats[side][cls].count}`}
                  aria-pressed={isPressed}
                  className={`history-stats-pane__value history-stats-pane__value--${cls} history-stats-pane__value--interactive${isActive ? ' history-stats-pane__value--active' : ''}`}
                  onMouseEnter={() => onStatHover(sel)}
                  onMouseLeave={() => onStatHover(null)}
                  onClick={() => onStatClick(sel)}
                >
                  {sideStats[side][cls].count}
                </button>
              );
            }),
          ];
        })}

        {/* Avg CPL row */}
        <div className="history-stats-pane__label">Avg CPL</div>
        {(['player', 'opponent'] as const).map((side) => {
          // Gate colour and value on the same null check: a genuine 0 is perfect play
          // and must stay coloured and printed as 0.
          const avgCpl = sideStats[side].avgCpl;
          return (
            <div
              key={`avgcpl-${side}`}
              className="history-stats-pane__value history-stats-pane__value--acpl"
              style={avgCpl !== null ? { color: acplColor(avgCpl) } : undefined}
            >
              {avgCpl ?? '—'}
            </div>
          );
        })}

        {/* Accuracy row (user only) */}
        <div className="history-stats-pane__label history-stats-pane__label--accuracy" ref={infoRef}>
          <span className="history-stats-pane__accuracy-copy history-stats-pane__accuracy-copy--full">
            Accuracy
          </span>
          <span
            className="history-stats-pane__accuracy-copy history-stats-pane__accuracy-copy--compact"
            aria-hidden="true"
          >
            Acc.
          </span>
          <button
            type="button"
            className="history-stats-pane__info-btn"
            aria-label="What does Accuracy mean?"
            aria-expanded={infoOpen}
            onClick={() => setInfoOpen((v) => !v)}
          >
            <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
              <circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" strokeWidth="1.4" />
              <circle cx="8" cy="4.6" r="0.95" fill="currentColor" />
              <rect x="7.2" y="6.6" width="1.6" height="5" rx="0.8" fill="currentColor" />
            </svg>
          </button>
          {infoOpen && (
            <div className="history-stats-pane__info-popup" role="tooltip">
              {ACCURACY_INFO.split('\n\n').map((para, i) => (
                <p key={i}>{para}</p>
              ))}
            </div>
          )}
        </div>
        <div
          className="history-stats-pane__value history-stats-pane__value--you"
          style={accuracy != null ? { color: accuracyColor(accuracy) } : undefined}
        >
          {accuracy != null
            ? `${accuracy}%`
            : accuracyPending
              ? <span className="history-stats-pane__value--pending">computing{'…'}</span>
              : '—'}
        </div>
        <div className="history-stats-pane__value" aria-hidden="true" />

        {/* Moves row */}
        <div className="history-stats-pane__label">Moves</div>
        <div className="history-stats-pane__value history-stats-pane__value--span">{totalMoves}</div>

      </div>
    </div>
  );
}

export default GameReviewStats;
