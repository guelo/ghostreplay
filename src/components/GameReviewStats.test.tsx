import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import GameReviewStats from './GameReviewStats';
import type { SideStats } from '../utils/gameStats';

const emptySide = (): SideStats => ({
  blunder: { count: 0, indices: [] },
  mistake: { count: 0, indices: [] },
  inaccuracy: { count: 0, indices: [] },
  avgCpl: 0,
});

function renderStats(accuracy: number | null, accuracyPending = false) {
  return render(
    <GameReviewStats
      sideStats={{ player: emptySide(), opponent: emptySide() }}
      activeStat={null}
      pinnedStat={null}
      totalMoves={10}
      accuracy={accuracy}
      accuracyPending={accuracyPending}
      onStatHover={vi.fn()}
      onStatClick={vi.fn()}
    />,
  );
}

describe('GameReviewStats accuracy row', () => {
  it('renders the accuracy value when present', () => {
    renderStats(87);
    expect(screen.getByText('Accuracy')).toBeInTheDocument();
    expect(screen.getByText('87%')).toBeInTheDocument();
  });

  it('renders a placeholder when accuracy is null', () => {
    renderStats(null);
    expect(screen.getByText('Accuracy')).toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('renders "computing…" when accuracy is null but still processing', () => {
    renderStats(null, true);
    expect(screen.getByText('Accuracy')).toBeInTheDocument();
    expect(screen.getByText('computing…')).toBeInTheDocument();
    expect(screen.queryByText('—')).not.toBeInTheDocument();
  });

  it('prefers the accuracy value over the pending state when both are set', () => {
    renderStats(87, true);
    expect(screen.getByText('87%')).toBeInTheDocument();
    expect(screen.queryByText('computing…')).not.toBeInTheDocument();
  });
});
