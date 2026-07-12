import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import GameReviewStats from './GameReviewStats';
import type { SideStats } from '../utils/gameStats';
import { accuracyColor, acplColor } from '../utils/statColor';

const emptySide = (over: Partial<SideStats> = {}): SideStats => ({
  blunder: { count: 0, indices: [] },
  mistake: { count: 0, indices: [] },
  inaccuracy: { count: 0, indices: [] },
  avgCpl: 0,
  avgCplCount: 0,
  ...over,
});

function renderStats(
  accuracy: number | null,
  accuracyPending = false,
  sides?: { player: SideStats; opponent: SideStats },
) {
  return render(
    <GameReviewStats
      sideStats={sides ?? { player: emptySide(), opponent: emptySide() }}
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

describe('GameReviewStats classification icons', () => {
  it('renders the movelist classification icon next to each label', () => {
    renderStats(87);
    // Blunder ??, Mistake ?, Inaccuracy ?! — rendered as decorative pills.
    const blunder = screen.getByRole('button', { name: /^your blunders$/i });
    const mistake = screen.getByRole('button', { name: /^your mistakes$/i });
    const inaccuracy = screen.getByRole('button', { name: /^your inaccuracies$/i });
    expect(blunder).toHaveTextContent('??');
    expect(mistake).toHaveTextContent('?');
    expect(inaccuracy).toHaveTextContent('?!');
    expect(blunder.querySelector('.move-icon--blunder')).not.toBeNull();
    expect(mistake.querySelector('.move-icon--mistake')).not.toBeNull();
    expect(inaccuracy.querySelector('.move-icon--inaccuracy')).not.toBeNull();
  });
});

describe('GameReviewStats gradient colors', () => {
  it('colors the accuracy value with accuracyColor', () => {
    renderStats(87);
    expect(screen.getByText('87%')).toHaveStyle({ color: accuracyColor(87) });
  });

  it('does not color accuracy when null', () => {
    renderStats(null);
    expect(screen.getByText('—')).not.toHaveStyle({ color: accuracyColor(80) });
  });

  it('colors the Avg CPL value with acplColor when analysis is complete', () => {
    renderStats(80, false, {
      player: emptySide({ avgCpl: 30, avgCplCount: 12 }),
      opponent: emptySide({ avgCpl: 70, avgCplCount: 12 }),
    });
    expect(screen.getByText('30')).toHaveStyle({ color: acplColor(30) });
    expect(screen.getByText('70')).toHaveStyle({ color: acplColor(70) });
  });

  it('leaves Avg CPL uncolored when no evaluated moves', () => {
    renderStats(null, false, {
      player: emptySide({ avgCpl: 0, avgCplCount: 0 }),
      opponent: emptySide({ avgCpl: 0, avgCplCount: 0 }),
    });
    const zeros = screen.getAllByText('0');
    for (const el of zeros) expect(el).not.toHaveStyle({ color: acplColor(0) });
  });

  it('colors Avg CPL immediately while accuracy is still computing', () => {
    renderStats(null, true, {
      player: emptySide({ avgCpl: 30, avgCplCount: 12 }),
      opponent: emptySide({ avgCpl: 70, avgCplCount: 12 }),
    });
    expect(screen.getByText('30')).toHaveStyle({ color: acplColor(30) });
    expect(screen.getByText('70')).toHaveStyle({ color: acplColor(70) });
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
    expect(tooltip).toHaveTextContent(/how closely your moves matched the engine/i);
    expect(tooltip).toHaveTextContent(/100% means perfect play/i);

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
