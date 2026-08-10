import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "../test/utils";
import OpeningSideToggle from "./OpeningSideToggle";

describe("OpeningSideToggle", () => {
  it.each([
    { playerColor: null, whitePressed: "false", blackPressed: "false" },
    {
      playerColor: "white" as const,
      whitePressed: "true",
      blackPressed: "false",
    },
    {
      playerColor: "black" as const,
      whitePressed: "false",
      blackPressed: "true",
    },
  ])(
    "reflects the controlled $playerColor selection",
    ({ playerColor, whitePressed, blackPressed }) => {
      render(
        <OpeningSideToggle
          playerColor={playerColor}
          onPlayerColorChange={vi.fn()}
        />,
      );

      const group = screen.getByRole("group", { name: "Playing as" });
      expect(
        within(group).getByRole("button", { name: "White" }),
      ).toHaveAttribute("aria-pressed", whitePressed);
      expect(
        within(group).getByRole("button", { name: "Black" }),
      ).toHaveAttribute("aria-pressed", blackPressed);
    },
  );

  it("reports both side choices", () => {
    const onPlayerColorChange = vi.fn();
    render(
      <OpeningSideToggle
        playerColor="white"
        onPlayerColorChange={onPlayerColorChange}
      />,
    );

    const group = screen.getByRole("group", { name: "Playing as" });
    fireEvent.click(within(group).getByRole("button", { name: "White" }));
    fireEvent.click(within(group).getByRole("button", { name: "Black" }));

    expect(onPlayerColorChange).toHaveBeenNthCalledWith(1, "white");
    expect(onPlayerColorChange).toHaveBeenNthCalledWith(2, "black");
  });

  it("disables both choices", () => {
    render(
      <OpeningSideToggle
        playerColor="white"
        onPlayerColorChange={vi.fn()}
        disabled
      />,
    );

    const group = screen.getByRole("group", { name: "Playing as" });
    expect(
      within(group).getByRole("button", { name: "White" }),
    ).toBeDisabled();
    expect(
      within(group).getByRole("button", { name: "Black" }),
    ).toBeDisabled();
  });
});
