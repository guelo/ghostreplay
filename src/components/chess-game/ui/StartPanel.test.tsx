import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import StartPanel from "./StartPanel";
import type { OpeningRootItem } from "../../../utils/api";

const adHocOpening: OpeningRootItem = {
  opening_key: "adhoc-fen",
  opening_name: "Custom line",
  opening_family: "",
  eco: null,
  depth: 2,
};

const registered: OpeningRootItem = {
  opening_key: "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -",
  opening_name: "Sicilian Defense",
  opening_family: "Sicilian",
  eco: "B20",
  depth: 1,
};

const baseProps = () => ({
  isDrillMode: false,
  isStartingGame: false,
  startError: null,
  onClose: vi.fn(),
  onSwitchToPlayMode: vi.fn(),
  onSwitchToDrillMode: vi.fn(),
  maiaEloBins: [800, 1000, 1200] as const,
  seedEngineElo: 1000,
  seedStrictnessCp: 25,
  seedColor: "white" as const,
  seedOpening: null as OpeningRootItem | null,
  seedLine: null as string[] | null,
  playerRating: 1200,
  isProvisional: false,
  openingFamilies: [{ family_name: "Sicilian", roots: [registered] }],
  isLoadingOpenings: false,
  onStartPlay: vi.fn(),
  onStartDrill: vi.fn(),
});

// Engine-difficulty slider has max = bins.length - 1; strictness slider max = 50.
const eloSlider = () =>
  screen.getAllByRole("slider").find((s) => s.getAttribute("max") === "2")!;
const strictnessSlider = () =>
  screen.getAllByRole("slider").find((s) => s.getAttribute("max") === "50")!;

describe("StartPanel", () => {
  it("drafts difficulty locally and commits it only on Start (play)", () => {
    const props = baseProps();
    render(<StartPanel {...props} />);

    // Dragging is local — it must not commit until a side button (Start) is hit.
    fireEvent.change(screen.getByRole("slider"), { target: { value: "2" } });
    expect(props.onStartPlay).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /play black/i }));
    expect(props.onStartPlay).toHaveBeenCalledWith("black", 1200);
  });

  it("commits dragged elo + strictness on Start Drill, keeping the seeded ad-hoc line", () => {
    const props = {
      ...baseProps(),
      isDrillMode: true,
      seedOpening: adHocOpening,
      seedLine: ["e2e4", "e7e5"],
    };
    render(<StartPanel {...props} />);

    fireEvent.change(eloSlider(), { target: { value: "2" } }); // bins[2] === 1200
    fireEvent.change(strictnessSlider(), { target: { value: "40" } });
    expect(props.onStartDrill).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /start drill/i }));
    expect(props.onStartDrill).toHaveBeenCalledWith({
      engineElo: 1200,
      strictnessCp: 40,
      playerColor: "white",
      opening: adHocOpening,
      line: ["e2e4", "e7e5"],
    });
  });

  it("drops the ad-hoc line when a registered opening is picked (Finding 2: no leak)", () => {
    const props = {
      ...baseProps(),
      isDrillMode: true,
      seedOpening: adHocOpening,
      seedLine: ["e2e4", "e7e5"],
    };
    render(<StartPanel {...props} />);

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: /Sicilian Defense/ }));

    fireEvent.click(screen.getByRole("button", { name: /start drill/i }));
    expect(props.onStartDrill).toHaveBeenCalledWith(
      expect.objectContaining({
        opening: expect.objectContaining({ opening_key: registered.opening_key }),
        line: null,
      }),
    );
  });

  it("resyncs the draft when a seed prop changes (async reseed)", () => {
    const props = { ...baseProps(), isDrillMode: true, seedOpening: registered };
    const { rerender } = render(<StartPanel {...props} />);
    expect(screen.getByText(/25 cp loss allowed/i)).toBeInTheDocument();

    rerender(<StartPanel {...props} seedStrictnessCp={45} />);
    expect(screen.getByText(/45 cp loss allowed/i)).toBeInTheDocument();
  });
});
