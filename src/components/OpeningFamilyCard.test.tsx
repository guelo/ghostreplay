import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import OpeningFamilyCard from "./OpeningFamilyCard";

function renderCard(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

vi.mock("react-chessboard", () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => (
    <div data-testid="card-board" data-position={options.position as string} />
  ),
}));

const baseProps = {
  openingName: "Ruy Lopez",
  openingKey: "ruy-key",
  playerColor: "white" as const,
  score: 72,
  coverage: 0.5,
  sampleSize: 10,
  confidence: 0.8,
  isUnscored: false,
};

describe("OpeningFamilyCard", () => {
  it("renders the full variant with move line and drill button", () => {
    const onStartDrill = vi.fn();
    renderCard(
      <OpeningFamilyCard
        {...baseProps}
        variant="full"
        moveLine="1.e4 e5 2.Nf3 Nc6 3.Bb5"
        footerNote="2 children"
        drillDownLabel="Drill down"
        onStartDrill={onStartDrill}
      />,
    );

    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(screen.getByText("1.e4 e5 2.Nf3 Nc6 3.Bb5")).toBeInTheDocument();
    expect(screen.getByText("2 children")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Start Drill" }),
    ).toBeInTheDocument();
    expect(screen.getByText("72")).toBeInTheDocument();
  });

  it("renders the analysis variant without the full move line", () => {
    renderCard(<OpeningFamilyCard {...baseProps} variant="analysis" />);

    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(screen.queryByText(/Moves:/)).not.toBeInTheDocument();
    // Metrics + board still render.
    expect(screen.getByTestId("card-board")).toHaveAttribute(
      "data-position",
      "ruy-key",
    );
    expect(screen.getByText("Coverage")).toBeInTheDocument();
  });

  it("renders the analysis footer link + Start Drill button", async () => {
    const user = userEvent.setup();
    const onStartDrill = vi.fn();
    renderCard(
      <OpeningFamilyCard
        {...baseProps}
        variant="analysis"
        openingsHref="/openings?color=white&opening=ruy-key"
        onStartDrill={onStartDrill}
      />,
    );

    expect(
      screen.getByRole("link", { name: /View in Openings/ }),
    ).toHaveAttribute("href", "/openings?color=white&opening=ruy-key");

    await user.click(screen.getByRole("button", { name: "Start Drill" }));
    expect(onStartDrill).toHaveBeenCalledTimes(1);
  });

  it("renders a collapse surface that fires onCollapse when clicked", async () => {
    const user = userEvent.setup();
    const onCollapse = vi.fn();
    renderCard(
      <OpeningFamilyCard
        {...baseProps}
        variant="analysis"
        onCollapse={onCollapse}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /Collapse Ruy Lopez details/ }),
    );
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it("shows an em-dash grade and unscored note for a null score", () => {
    renderCard(
      <OpeningFamilyCard
        {...baseProps}
        variant="analysis"
        score={null}
        isUnscored
      />,
    );

    expect(screen.getByText("—")).toBeInTheDocument();
    expect(
      screen.getByText("No scored roots in this subtree yet."),
    ).toBeInTheDocument();
  });
});
