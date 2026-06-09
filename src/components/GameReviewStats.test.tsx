import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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

describe('GameReviewStats accuracy info button', () => {
  it('toggles an explanatory popup when the info button is clicked', async () => {
    const user = userEvent.setup();
    renderStats(87);

    const btn = screen.getByRole('button', { name: /what does accuracy mean/i });
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    await user.click(btn);
    const tooltip = screen.getByRole('tooltip');
    expect(tooltip).toHaveTextContent(/overall measure of your play/i);
    expect(tooltip).toHaveTextContent(/100% means every move matched the engine/i);

    await user.click(btn);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('closes the popup when clicking outside', async () => {
    const user = userEvent.setup();
    renderStats(87);

    await user.click(screen.getByRole('button', { name: /what does accuracy mean/i }));
    expect(screen.getByRole('tooltip')).toBeInTheDocument();

    await user.click(document.body);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });
});
