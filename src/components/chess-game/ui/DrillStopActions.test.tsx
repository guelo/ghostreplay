import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import DrillStopActions from "./DrillStopActions";

describe("DrillStopActions", () => {
  const baseProps = {
    terminalReason: "accuracy" as const,
    onAnotherDrill: vi.fn(),
    onAnalyze: vi.fn(),
    analyzeEnabled: true,
    isPreparing: false,
  };

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
});
