import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import type { ReactNode } from "react";
import DrillAnalysisPage from "./DrillAnalysisPage";
import { useDrillAnalysisStore } from "../stores/drillAnalysisStore";
import type { DrillAnalysisSnapshot } from "../components/chess-game/domain/sessionUpload";

vi.mock("../components/AnalysisBoard", () => ({
  default: ({
    boardOrientation,
    initialMoveIndex,
    footer,
  }: {
    boardOrientation: string;
    initialMoveIndex?: number;
    footer?: ReactNode;
  }) => (
    <div
      data-testid="analysis-board"
      data-orientation={boardOrientation}
      data-initial-move={initialMoveIndex}
    >
      {footer}
    </div>
  ),
}));

vi.mock("../components/AppNav", () => ({
  default: () => <nav data-testid="app-nav" />,
}));

function renderWithRoutes() {
  return render(
    <MemoryRouter initialEntries={["/drill-analysis"]}>
      <Routes>
        <Route path="/drill-analysis" element={<DrillAnalysisPage />} />
        <Route path="/play" element={<div data-testid="play-page" />} />
      </Routes>
    </MemoryRouter>,
  );
}

const snapshot: DrillAnalysisSnapshot = {
  moves: [
    {
      move_number: 1,
      color: "white",
      move_san: "e4",
      fen_after: "after",
      eval_cp: 20,
      eval_mate: null,
      best_move_san: "d4",
      best_move_eval_cp: 30,
      eval_delta: 10,
      classification: "good",
    },
  ],
  positionAnalysis: {},
  playerColor: "black",
  initialMoveIndex: 0,
};

describe("DrillAnalysisPage", () => {
  beforeEach(() => {
    useDrillAnalysisStore.getState().clear();
  });

  it("renders the AnalysisBoard from the store snapshot", () => {
    useDrillAnalysisStore.getState().setSnapshot(snapshot);
    renderWithRoutes();

    const board = screen.getByTestId("analysis-board");
    expect(board).toHaveAttribute("data-orientation", "black");
    expect(board).toHaveAttribute("data-initial-move", "0");
    expect(screen.getByText(/not saved/i)).toBeInTheDocument();
  });

  it("surfaces the partial-analysis warning when present", () => {
    useDrillAnalysisStore
      .getState()
      .setSnapshot({ ...snapshot, warning: "Analysis unavailable; showing partial review." });
    renderWithRoutes();

    expect(screen.getByText(/partial review/i)).toBeInTheDocument();
  });

  it("redirects to /play when the store is empty", () => {
    renderWithRoutes();
    expect(screen.getByTestId("play-page")).toBeInTheDocument();
    expect(screen.queryByTestId("analysis-board")).toBeNull();
  });
});
