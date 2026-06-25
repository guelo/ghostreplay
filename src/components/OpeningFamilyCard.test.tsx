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
  gameCount: 10,
  confidence: 0.8,
  isUnscored: false,
};

describe("OpeningFamilyCard", () => {
  it("renders the opening name, score, and Start Drill button", () => {
    const onStartDrill = vi.fn();
    renderCard(
      <OpeningFamilyCard {...baseProps} onStartDrill={onStartDrill} />,
    );

    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Start Drill" }),
    ).toBeInTheDocument();
    expect(screen.getByText("72")).toBeInTheDocument();
  });

  it("renders the board and metrics without a move line", () => {
    renderCard(<OpeningFamilyCard {...baseProps} />);

    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(screen.queryByText(/Moves:/)).not.toBeInTheDocument();
    // Metrics + board still render.
    expect(screen.getByTestId("card-board")).toHaveAttribute(
      "data-position",
      "ruy-key",
    );
    expect(screen.getByText("Coverage")).toBeInTheDocument();
  });

  it("renders the footer link + Start Drill button", async () => {
    const user = userEvent.setup();
    const onStartDrill = vi.fn();
    renderCard(
      <OpeningFamilyCard
        {...baseProps}
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
      <OpeningFamilyCard {...baseProps} onCollapse={onCollapse} />,
    );

    await user.click(
      screen.getByRole("button", { name: /Collapse Ruy Lopez details/ }),
    );
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it("shows an em-dash grade and unscored note for a null score", () => {
    renderCard(
      <OpeningFamilyCard {...baseProps} score={null} isUnscored />,
    );

    expect(screen.getByText("—")).toBeInTheDocument();
    expect(
      screen.getByText("No scored roots in this subtree yet."),
    ).toBeInTheDocument();
  });
});
