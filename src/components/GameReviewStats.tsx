import { CLASS_KEYS, type SideStats, type StatSelection, type ClassKey } from '../utils/gameStats';

interface GameReviewStatsProps {
  sideStats: { player: SideStats; opponent: SideStats };
  activeStat: StatSelection;
  pinnedStat: StatSelection;
  totalMoves: number;
  onStatHover: (sel: StatSelection) => void;
  onStatClick: (sel: StatSelection) => void;
}

function GameReviewStats({ sideStats, activeStat, pinnedStat, totalMoves, onStatHover, onStatClick }: GameReviewStatsProps) {
  return (
    <div className="history-stats-pane">
      <div className="history-stats-pane__grid">
        {/* Header row */}
        <div className="history-stats-pane__header" />
        <div className="history-stats-pane__header">You</div>
        <div className="history-stats-pane__header">Ghost</div>

        {/* Classification rows */}
        {CLASS_KEYS.map((cls: ClassKey) => {
          const label = cls === 'inaccuracy' ? 'Inaccur.' : cls.charAt(0).toUpperCase() + cls.slice(1) + 's';
          const fullLabel = cls === 'inaccuracy' ? 'Inaccuracies' : cls.charAt(0).toUpperCase() + cls.slice(1) + 's';
          const labelSel = { side: 'player' as const, cls };
          const isLabelActive = activeStat?.cls === cls;
          const isLabelPressed = pinnedStat?.side === 'player' && pinnedStat?.cls === cls;
          return [
            <button
              key={`${cls}-label`}
              type="button"
              aria-label={`Your ${fullLabel}`}
              aria-pressed={isLabelPressed}
              className={`history-stats-pane__label history-stats-pane__label--${cls} history-stats-pane__label--interactive${isLabelActive ? ' history-stats-pane__label--active' : ''}`}
              onMouseEnter={() => onStatHover(labelSel)}
              onMouseLeave={() => onStatHover(null)}
              onClick={() => onStatClick(labelSel)}
            >
              {label}
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
                  aria-label={`${sideLabel} ${fullLabel}: ${sideStats[side][cls].count}`}
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
        <div className="history-stats-pane__value">{sideStats.player.avgCpl}</div>
        <div className="history-stats-pane__value">{sideStats.opponent.avgCpl}</div>

        {/* Moves row */}
        <div className="history-stats-pane__label">Moves</div>
        <div className="history-stats-pane__value history-stats-pane__value--span">{totalMoves}</div>
      </div>
    </div>
  );
}

export default GameReviewStats;
