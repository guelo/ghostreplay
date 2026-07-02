import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "../../../test/utils";
import { fireEvent } from "@testing-library/react";
import EndGameFanfare, {
  END_GAME_FANFARE_HOLD_MS,
  END_GAME_FANFARE_SHRINK_MS,
  type EndGameFanfareTrigger,
} from "./EndGameFanfare";
import type { GameResult } from "../domain/status";

const trigger = (result: GameResult, id = 1): EndGameFanfareTrigger => ({
  id,
  result,
});

describe("EndGameFanfare", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders the win headline + reason with the win variant class", () => {
    const { container } = render(
      <EndGameFanfare
        trigger={trigger({
          type: "checkmate_win",
          message: "x",
          reason: "checkmate",
        })}
        onDone={vi.fn()}
      />,
    );
    expect(container.querySelector(".end-game-fanfare--win")).not.toBeNull();
    expect(
      container.querySelector(".end-game-fanfare__headline")?.textContent,
    ).toBe("Victory");
    expect(
      container.querySelector(".end-game-fanfare__reason")?.textContent,
    ).toBe("Checkmate");
  });

  it("renders the loss variant for a resignation", () => {
    const { container } = render(
      <EndGameFanfare
        trigger={trigger({
          type: "resign",
          message: "x",
          reason: "resignation",
        })}
        onDone={vi.fn()}
      />,
    );
    expect(container.querySelector(".end-game-fanfare--loss")).not.toBeNull();
    expect(
      container.querySelector(".end-game-fanfare__headline")?.textContent,
    ).toBe("Defeat");
    expect(
      container.querySelector(".end-game-fanfare__reason")?.textContent,
    ).toBe("Resignation");
  });

  it("renders the draw variant and names the termination type", () => {
    const { container } = render(
      <EndGameFanfare
        trigger={trigger({
          type: "draw",
          message: "x",
          reason: "fifty_move",
        })}
        onDone={vi.fn()}
      />,
    );
    expect(container.querySelector(".end-game-fanfare--draw")).not.toBeNull();
    expect(
      container.querySelector(".end-game-fanfare__headline")?.textContent,
    ).toBe("Draw");
    expect(
      container.querySelector(".end-game-fanfare__reason")?.textContent,
    ).toBe("Fifty-move rule");
  });

  it("auto-dismisses and calls onDone after the hold + shrink window", () => {
    const onDone = vi.fn();
    const { container } = render(
      <EndGameFanfare
        trigger={trigger({
          type: "checkmate_win",
          message: "x",
          reason: "checkmate",
        })}
        onDone={onDone}
      />,
    );

    expect(container.querySelector(".end-game-fanfare")).not.toBeNull();

    act(() => {
      vi.advanceTimersByTime(END_GAME_FANFARE_HOLD_MS + 10);
    });
    // Now shrinking, not yet done.
    expect(container.querySelector(".end-game-fanfare--shrink")).not.toBeNull();
    expect(onDone).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(END_GAME_FANFARE_SHRINK_MS + 20);
    });
    expect(container.querySelector(".end-game-fanfare")).toBeNull();
    expect(onDone).toHaveBeenCalledWith(1);
  });

  it("skips to shrink early on click", () => {
    const onDone = vi.fn();
    const { container } = render(
      <EndGameFanfare
        trigger={trigger({
          type: "draw",
          message: "x",
          reason: "stalemate",
        })}
        onDone={onDone}
      />,
    );

    const card = container.querySelector(".end-game-fanfare") as HTMLElement;
    act(() => {
      fireEvent.click(card);
    });
    expect(container.querySelector(".end-game-fanfare--shrink")).not.toBeNull();
    expect(onDone).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(END_GAME_FANFARE_SHRINK_MS + 20);
    });
    expect(onDone).toHaveBeenCalledWith(1);
  });

  it("does not resurrect the overlay when the original hold elapses after an early skip", () => {
    // Stable trigger + an onDone that never nulls the trigger, mirroring
    // BoardStage's default no-op callback. Without the hold-timer guard, the
    // original 2.4s timer would fire after the early dismiss and re-enter shrink,
    // flashing the overlay back and calling onDone a second time.
    const onDone = vi.fn();
    const { container } = render(
      <EndGameFanfare
        trigger={trigger({ type: "draw", message: "x", reason: "stalemate" })}
        onDone={onDone}
      />,
    );

    act(() => {
      fireEvent.click(container.querySelector(".end-game-fanfare") as HTMLElement);
    });
    act(() => {
      vi.advanceTimersByTime(END_GAME_FANFARE_SHRINK_MS + 20);
    });
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(container.querySelector(".end-game-fanfare")).toBeNull();

    // Past the original hold deadline: no flash-back, no second onDone.
    act(() => {
      vi.advanceTimersByTime(END_GAME_FANFARE_HOLD_MS + 20);
    });
    expect(container.querySelector(".end-game-fanfare")).toBeNull();
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("exposes a real focusable button as the dismiss control (keyboard path)", () => {
    const onDone = vi.fn();
    const { container } = render(
      <EndGameFanfare
        trigger={trigger({
          type: "checkmate_win",
          message: "x",
          reason: "checkmate",
        })}
        onDone={onDone}
      />,
    );

    // The dismiss target is a real <button>, so it is focusable and natively
    // keyboard-operable (Enter/Space) rather than a mouse-only clickable div.
    const dismiss = container.querySelector(
      ".end-game-fanfare__inner",
    ) as HTMLElement;
    expect(dismiss.tagName).toBe("BUTTON");
    dismiss.focus();
    expect(dismiss).toHaveFocus();

    // Activating the button (native Enter/Space dispatches this click) dismisses.
    act(() => {
      fireEvent.click(dismiss);
    });
    expect(container.querySelector(".end-game-fanfare--shrink")).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(END_GAME_FANFARE_SHRINK_MS + 20);
    });
    expect(onDone).toHaveBeenCalledWith(1);
  });

  it("renders nothing without a trigger", () => {
    const { container } = render(
      <EndGameFanfare trigger={null} onDone={vi.fn()} />,
    );
    expect(container.querySelector(".end-game-fanfare")).toBeNull();
  });

  it("restarts the window when a new trigger id arrives mid-display", () => {
    const onDone = vi.fn();
    const { container, rerender } = render(
      <EndGameFanfare
        trigger={trigger(
          { type: "checkmate_win", message: "x", reason: "checkmate" },
          1,
        )}
        onDone={onDone}
      />,
    );

    // Part-way through the first hold, a new end (loss) supersedes it.
    act(() => {
      vi.advanceTimersByTime(END_GAME_FANFARE_HOLD_MS - 200);
    });
    rerender(
      <EndGameFanfare
        trigger={trigger(
          { type: "checkmate_loss", message: "x", reason: "checkmate" },
          2,
        )}
        onDone={onDone}
      />,
    );

    // The card now shows the new outcome and is back in the hold phase.
    expect(container.querySelector(".end-game-fanfare--loss")).not.toBeNull();
    expect(container.querySelector(".end-game-fanfare--shrink")).toBeNull();

    // The first window's original deadline passes without firing onDone — the
    // timer was restarted for id 2.
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(onDone).not.toHaveBeenCalled();

    // The full new window elapses and reports id 2 (hold → shrink in one step,
    // shrink → done in the next so the shrink effect flushes between).
    act(() => {
      vi.advanceTimersByTime(END_GAME_FANFARE_HOLD_MS + 10);
    });
    act(() => {
      vi.advanceTimersByTime(END_GAME_FANFARE_SHRINK_MS + 40);
    });
    expect(onDone).toHaveBeenCalledWith(2);
    expect(onDone).not.toHaveBeenCalledWith(1);
  });
});
