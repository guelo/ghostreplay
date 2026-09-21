import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import DrillStopActions from "./DrillStopActions";

describe("DrillStopActions", () => {
  const baseProps = {
    terminalReason: "accuracy" as const,
    onAnotherDrill: vi.fn(),
    onAnotherDrillSettings: vi.fn(),
    onAnalyze: vi.fn(),
    analyzeEnabled: true,
    isPreparing: false,
  };

  it("renders a gear button and fires onAnotherDrillSettings", () => {
    const onAnotherDrillSettings = vi.fn();
    render(
      <DrillStopActions {...baseProps} onAnotherDrillSettings={onAnotherDrillSettings} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Change drill settings" }));
    expect(onAnotherDrillSettings).toHaveBeenCalledTimes(1);
  });

  it("disables Again and the gear when disabled", () => {
    render(<DrillStopActions {...baseProps} disabled />);

    expect(screen.getByRole("button", { name: "Again" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Change drill settings" })).toBeDisabled();
  });

  it("keeps a pending Again event-capable while settings and Analyze stay usable", () => {
    const onAnotherDrill = vi.fn();
    const onAnotherDrillSettings = vi.fn();
    const onAnalyze = vi.fn();
    render(
      <DrillStopActions
        {...baseProps}
        drillAgainPending
        onAnotherDrill={onAnotherDrill}
        onAnotherDrillSettings={onAnotherDrillSettings}
        onAnalyze={onAnalyze}
      />,
    );

    const waiting = screen.getByRole("button", {
      name: "Updating score before another drill",
    });
    expect(waiting).toHaveTextContent("Updating score…");
    expect(waiting).toHaveAttribute("aria-disabled", "true");
    expect(waiting).toHaveAttribute("aria-busy", "true");
    expect(waiting).not.toBeDisabled();

    fireEvent.click(waiting, { detail: 1 });
    fireEvent.click(waiting, { detail: 0 });
    fireEvent.click(screen.getByRole("button", { name: "Change drill settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Analyze" }));

    expect(onAnotherDrill).toHaveBeenCalledTimes(2);
    expect(onAnotherDrillSettings).toHaveBeenCalledTimes(1);
    expect(onAnalyze).toHaveBeenCalledTimes(1);
  });

  it("renders an Analyze button and fires onAnalyze", () => {
    const onAnalyze = vi.fn();
    render(<DrillStopActions {...baseProps} onAnalyze={onAnalyze} />);

    const button = screen.getByRole("button", { name: "Analyze" });
    fireEvent.click(button);
    expect(onAnalyze).toHaveBeenCalledTimes(1);
  });

  it("shows the preparing state and disables the button", () => {
    render(<DrillStopActions {...baseProps} isPreparing />);

    const button = screen.getByRole("button", { name: "Preparing analysis…" });
    expect(button).toBeDisabled();
    expect(screen.getByRole("button", { name: "Again" })).toBeDisabled();
  });

  it("hides the Analyze button when there are no moves to review", () => {
    render(<DrillStopActions {...baseProps} analyzeEnabled={false} />);

    expect(screen.queryByRole("button", { name: "Analyze" })).toBeNull();
    expect(screen.getByRole("button", { name: "Again" })).toBeInTheDocument();
  });

  it("renders an error message when provided", () => {
    render(
      <DrillStopActions {...baseProps} errorMessage="Couldn't end the drill. Try Analyze again." />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn't end the drill/i);
  });

  it("does not offer the old continue-as-game action", () => {
    render(<DrillStopActions {...baseProps} />);
    expect(screen.queryByText(/continue as normal game/i)).toBeNull();
  });

  it("shows a fail banner with the warning icon for an accuracy stop", () => {
    const { container } = render(
      <DrillStopActions {...baseProps} terminalReason="accuracy" />,
    );

    const banner = container.querySelector(".drill-stop-banner--fail");
    expect(banner).not.toBeNull();
    expect(
      banner?.querySelector(".drill-stop-banner__headline")?.textContent,
    ).toBe("Bad move");
    expect(
      screen.getByText("That move exceeded this drill's centipawn limit"),
    ).toBeInTheDocument();
    expect(banner?.querySelector(".warning-triangle-icon")).not.toBeNull();
  });

  it("shows a fail banner for an off-route stop", () => {
    const { container } = render(
      <DrillStopActions {...baseProps} terminalReason="off_route" />,
    );

    expect(container.querySelector(".drill-stop-banner--fail")).not.toBeNull();
    expect(
      container.querySelector(".drill-stop-banner__headline")?.textContent,
    ).toBe("Off route");
    expect(
      screen.getByText("That's not how you get to the opening"),
    ).toBeInTheDocument();
  });

  it.each([["natural_end" as const], [null]])(
    "shows a neutral banner with no icon or detail for %s",
    (terminalReason) => {
      const { container } = render(
        <DrillStopActions {...baseProps} terminalReason={terminalReason} />,
      );

      const banner = container.querySelector(".drill-stop-banner--neutral");
      expect(banner).not.toBeNull();
      expect(
        banner?.querySelector(".drill-stop-banner__headline")?.textContent,
      ).toBe("Drill stopped.");
      expect(banner?.querySelector(".warning-triangle-icon")).toBeNull();
      expect(container.querySelector(".drill-stop-banner__detail")).toBeNull();
    },
  );

  it("keeps the region accessible name the e2e locator depends on", () => {
    render(<DrillStopActions {...baseProps} />);
    expect(
      screen.getByRole("region", {
        name: "Drill stopped — choose next action",
      }),
    ).toBeInTheDocument();
  });
});
