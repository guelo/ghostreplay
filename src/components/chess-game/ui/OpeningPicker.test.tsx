import { act } from "react";
import { describe, expect, it, vi } from "vitest";
import { Chess } from "chess.js";
import { fireEvent, render, screen } from "../../../test/utils";
import OpeningPicker from "./OpeningPicker";
import { normalize_fen } from "../../../utils/fen";
import type { OpeningRootItem } from "../../../utils/api";

// Capture the live onPieceDrop so board tests can drive arbitrary moves.
let lastPieceDrop:
  | ((args: { sourceSquare: string; targetSquare: string }) => boolean)
  | null = null;

vi.mock("react-chessboard", () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => {
    lastPieceDrop = options.onPieceDrop as typeof lastPieceDrop;
    return <div data-testid="board" data-position={options.position as string} />;
  },
}));

const drop = (sourceSquare: string, targetSquare: string) =>
  act(() => {
    lastPieceDrop?.({ sourceSquare, targetSquare });
  });

// Build real 4-field opening_keys from real games so they match what the picker
// computes from board moves (avoids hardcoding en-passant edge cases).
const keyAfter = (sans: string[]): string => {
  const chess = new Chess();
  sans.forEach((san) => chess.move(san));
  return normalize_fen(chess.fen());
};

const KINGS_PAWN_KEY = keyAfter(["e4"]);
const CARO_KANN_KEY = keyAfter(["e4", "c6"]);

const makeFamilies = (): Array<{ family_name: string; roots: OpeningRootItem[] }> => [
  {
    family_name: "King's Pawn",
    roots: [
      {
        opening_key: KINGS_PAWN_KEY,
        opening_name: "King's Pawn Game",
        opening_family: "King's Pawn",
        eco: "B00",
        depth: 1,
      },
      {
        opening_key: CARO_KANN_KEY,
        opening_name: "Caro-Kann Defense",
        opening_family: "King's Pawn",
        eco: "B10",
        depth: 2,
      },
    ],
  },
  {
    family_name: "Queen's Pawn",
    roots: [
      {
        opening_key: keyAfter(["d4"]),
        opening_name: "Queen's Pawn Game",
        opening_family: "Queen's Pawn",
        eco: "A40",
        depth: 1,
      },
    ],
  },
];

describe("OpeningPicker", () => {
  it("filters the list by search query", () => {
    const onSelect = vi.fn();
    render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.change(screen.getByPlaceholderText(/search openings/i), {
      target: { value: "caro" },
    });

    expect(screen.getByRole("option", { name: /Caro-Kann/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Queen's Pawn Game/ })).not.toBeInTheDocument();
  });

  it("selects a row on click", () => {
    const onSelect = vi.fn();
    render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: /Caro-Kann/ }));

    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ opening_key: CARO_KANN_KEY }),
    );
  });

  it("closes on Escape", () => {
    render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes on outside click", () => {
    render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("renders the popover in a portal on document.body", () => {
    const { container } = render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    const popover = document.getElementById("opening-picker-popover");
    expect(popover).toBeInTheDocument();
    expect(container.contains(popover)).toBe(false);
  });

  it("navigates with arrow keys and selects with Enter", () => {
    const onSelect = vi.fn();
    render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    const search = screen.getByPlaceholderText(/search openings/i);
    // Active starts on first root (King's Pawn Game); ArrowDown -> Caro-Kann.
    fireEvent.keyDown(search, { key: "ArrowDown" });
    fireEvent.keyDown(search, { key: "Enter" });

    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ opening_key: CARO_KANN_KEY }),
    );
  });

  it("resolves board moves e4 then c6 to King's Pawn then Caro-Kann", () => {
    const onSelect = vi.fn();
    render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("tab", { name: /board/i }));

    drop("e2", "e4");
    expect(onSelect).toHaveBeenLastCalledWith(
      expect.objectContaining({ opening_key: KINGS_PAWN_KEY }),
    );
    expect(screen.getByText("B00 — King's Pawn Game")).toBeInTheDocument();

    drop("c7", "c6");
    expect(onSelect).toHaveBeenLastCalledWith(
      expect.objectContaining({ opening_key: CARO_KANN_KEY }),
    );
    expect(screen.getByText("B10 — Caro-Kann Defense")).toBeInTheDocument();
  });

  it("rejects an illegal drop and keeps the prior selection", () => {
    const onSelect = vi.fn();
    render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("tab", { name: /board/i }));

    drop("e2", "e4");
    onSelect.mockClear();

    // e2 is now empty — an illegal move must return false and not re-select.
    let result: boolean | undefined;
    act(() => {
      result = lastPieceDrop?.({ sourceSquare: "e2", targetSquare: "e5" });
    });
    expect(result).toBe(false);
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByText("B00 — King's Pawn Game")).toBeInTheDocument();
  });

  it("undoes and resets board moves", () => {
    const onSelect = vi.fn();
    render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("tab", { name: /board/i }));

    drop("e2", "e4");
    drop("c7", "c6");
    expect(screen.getByText("B10 — Caro-Kann Defense")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /undo/i }));
    expect(screen.getByText("B00 — King's Pawn Game")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /reset/i }));
    expect(screen.queryByText(/Caro-Kann|King's Pawn Game/)).not.toBeInTheDocument();
  });

  it("filters then selects with Enter after the active row is filtered out", () => {
    const onSelect = vi.fn();
    render(
      <OpeningPicker
        openingFamilies={makeFamilies()}
        selectedOpening={null}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    // Active starts on King's Pawn Game; filtering to "caro" removes it.
    fireEvent.change(screen.getByPlaceholderText(/search openings/i), {
      target: { value: "caro" },
    });
    fireEvent.keyDown(screen.getByPlaceholderText(/search openings/i), { key: "Enter" });

    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ opening_key: CARO_KANN_KEY }),
    );
  });

  it("shows a failed state and stays closed when families fail to load", () => {
    render(
      <OpeningPicker
        openingFamilies={null}
        selectedOpening={null}
        isLoading={false}
        onSelect={vi.fn()}
      />,
    );
    const trigger = screen.getByRole("combobox");
    expect(trigger).toBeDisabled();
    expect(screen.getByText(/failed to load openings/i)).toBeInTheDocument();
    fireEvent.click(trigger);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("disables the trigger while loading", () => {
    render(
      <OpeningPicker
        openingFamilies={null}
        selectedOpening={null}
        isLoading
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.getByText(/loading openings/i)).toBeInTheDocument();
  });
});
