import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import GameSelector from "./GameSelector";
import { formatShortDate, resultClass } from "./GameSelector.helpers";
import type { HistoryGame } from "../utils/api";

function makeGame(overrides: Partial<HistoryGame>): HistoryGame {
  return {
    session_id: "s1",
    started_at: "2026-06-05T10:00:00Z",
    ended_at: "2026-06-05T11:00:00Z",
    result: "checkmate_win",
    engine_elo: 1500,
    player_color: "white",
    opening_name: "Sicilian Defense",
    summary: {
      total_moves: 40,
      blunders: 1,
      mistakes: 2,
      inaccuracies: 3,
      average_centipawn_loss: 20,
      accuracy: 80,
    },
    ...overrides,
  };
}

describe("formatShortDate", () => {
  it("formats en-US as mm/dd/yy", () => {
    const orig = Date.prototype.toLocaleDateString;
    const spy = vi
      .spyOn(Date.prototype, "toLocaleDateString")
      .mockImplementation(function (this: Date, _locale, opts) {
        return orig.call(this, "en-US", opts);
      });
    expect(formatShortDate("2026-06-05T11:00:00Z")).toMatch(/06\/05\/26/);
    spy.mockRestore();
  });

  it("formats en-GB as dd/mm/yy", () => {
    const orig = Date.prototype.toLocaleDateString;
    const spy = vi
      .spyOn(Date.prototype, "toLocaleDateString")
      .mockImplementation(function (this: Date, _locale, opts) {
        return orig.call(this, "en-GB", opts);
      });
    expect(formatShortDate("2026-06-05T11:00:00Z")).toMatch(/05\/06\/26/);
    spy.mockRestore();
  });
});

describe("resultClass", () => {
  it("maps results to win/loss/draw", () => {
    expect(resultClass("checkmate_win")).toBe("win");
    expect(resultClass("checkmate_loss")).toBe("loss");
    expect(resultClass("resign")).toBe("loss");
    expect(resultClass("draw")).toBe("draw");
  });
});

describe("GameSelector", () => {
  it("shows opening name and no move count in rows", async () => {
    const user = userEvent.setup();
    const games = [
      makeGame({ session_id: "s1", opening_name: "Sicilian Defense" }),
    ];
    render(<GameSelector games={games} selectedId="s1" onChange={() => {}} />);

    await user.click(screen.getByRole("button"));
    const option = screen.getByRole("option");
    expect(within(option).getByText("Sicilian Defense")).toBeInTheDocument();
    expect(option.textContent).not.toMatch(/moves/i);
  });

  it("renders '—' when opening name is null", async () => {
    const user = userEvent.setup();
    const games = [makeGame({ session_id: "s1", opening_name: null })];
    render(<GameSelector games={games} selectedId="s1" onChange={() => {}} />);

    await user.click(screen.getByRole("button"));
    expect(within(screen.getByRole("option")).getByText("—")).toBeInTheDocument();
  });

  it("applies win/loss/draw color classes", async () => {
    const user = userEvent.setup();
    const games = [
      makeGame({ session_id: "w", result: "checkmate_win" }),
      makeGame({ session_id: "l", result: "resign" }),
      makeGame({ session_id: "d", result: "draw" }),
    ];
    render(<GameSelector games={games} selectedId="w" onChange={() => {}} />);
    await user.click(screen.getByRole("button"));

    const options = screen.getAllByRole("option");
    expect(options[0].className).toMatch(/custom-dropdown__row-inner--win/);
    expect(options[1].className).toMatch(/custom-dropdown__row-inner--loss/);
    expect(options[2].className).toMatch(/custom-dropdown__row-inner--draw/);
  });

  it("does not fire onChange while navigating with arrow keys (only on Enter)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const games = [
      makeGame({ session_id: "s1" }),
      makeGame({ session_id: "s2", opening_name: "French Defense" }),
      makeGame({ session_id: "s3", opening_name: "Caro-Kann Defense" }),
    ];
    render(<GameSelector games={games} selectedId="s1" onChange={onChange} />);

    const trigger = screen.getByRole("button");
    trigger.focus();
    await user.keyboard("{ArrowDown}"); // open, active = s1
    await user.keyboard("{ArrowDown}"); // active = s2
    expect(onChange).not.toHaveBeenCalled();

    await user.keyboard("{Enter}"); // confirm s2
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("s2");
  });

  it("Escape closes the popup without selecting", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const games = [makeGame({ session_id: "s1" }), makeGame({ session_id: "s2" })];
    render(<GameSelector games={games} selectedId="s1" onChange={onChange} />);

    const trigger = screen.getByRole("button");
    await user.click(trigger);
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    await user.keyboard("{ArrowDown}{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("fires onChange when an option is selected", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const games = [
      makeGame({ session_id: "s1" }),
      makeGame({ session_id: "s2", opening_name: "French Defense" }),
    ];
    render(<GameSelector games={games} selectedId="s1" onChange={onChange} />);

    await user.click(screen.getByRole("button"));
    await user.click(screen.getByText("French Defense"));
    expect(onChange).toHaveBeenCalledWith("s2");
  });
});
