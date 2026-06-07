import { describe, expect, it, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import ControlsRow from "./ControlsRow";

describe("ControlsRow", () => {
  it("renders nothing when no actions are provided", () => {
    const { container } = render(<ControlsRow />);
    expect(container.firstChild).toBeNull();
  });

  it("fires onResign / onFlipBoard / onReset callbacks", () => {
    const onResign = vi.fn();
    const onFlipBoard = vi.fn();
    const onReset = vi.fn();
    const { getByTitle } = render(
      <ControlsRow onResign={onResign} onFlipBoard={onFlipBoard} onReset={onReset} />,
    );
    fireEvent.click(getByTitle("Resign"));
    fireEvent.click(getByTitle("Flip board"));
    fireEvent.click(getByTitle("Reset game"));
    expect(onResign).toHaveBeenCalled();
    expect(onFlipBoard).toHaveBeenCalled();
    expect(onReset).toHaveBeenCalled();
  });

  it("disables the resign button when isResignDisabled", () => {
    const { getByTitle } = render(
      <ControlsRow onResign={vi.fn()} isResignDisabled />,
    );
    expect((getByTitle("Resign") as HTMLButtonElement).disabled).toBe(true);
  });

  it("only shows revert when the game is active", () => {
    const { queryByTitle, rerender } = render(
      <ControlsRow onRevert={vi.fn()} isGameActive={false} />,
    );
    expect(queryByTitle("Revert last move")).toBeNull();
    rerender(<ControlsRow onRevert={vi.fn()} isGameActive />);
    expect(queryByTitle("Revert last move")).not.toBeNull();
  });

  it("enables add only for a valid selected move", () => {
    const onAdd = vi.fn();
    const { getByTitle } = render(
      <ControlsRow
        onAddSelectedMove={onAdd}
        canAddSelectedMove
        effectiveIndex={3}
      />,
    );
    const btn = getByTitle("Add selected move to ghost library") as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    fireEvent.click(btn);
    expect(onAdd).toHaveBeenCalledWith(3);
  });
});
