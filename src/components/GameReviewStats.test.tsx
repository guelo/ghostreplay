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

function renderStats(accuracy: number | null) {
  return render(
    <GameReviewStats
      sideStats={{ player: emptySide(), opponent: emptySide() }}
      activeStat={null}
      pinnedStat={null}
      totalMoves={10}
      accuracy={accuracy}
      onStatHover={vi.fn()}
      onStatClick={vi.fn()}
    />,
  );
}

describe('GameReviewStats accuracy row', () => {
  it('renders the accuracy value when present', () => {
    renderStats(87);
    expect(screen.getByText('Accuracy')).toBeInTheDocument();
    expect(screen.getByText('87')).toBeInTheDocument();
  });

  it('renders a placeholder when accuracy is null', () => {
    renderStats(null);
    expect(screen.getByText('Accuracy')).toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
