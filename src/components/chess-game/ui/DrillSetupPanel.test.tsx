import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import DrillSetupPanel from "./DrillSetupPanel";

const makeProps = () => {
  const onSelectOpening = vi.fn();
  const onPlayerColorChange = vi.fn();
  const onEngineEloChange = vi.fn();
  const onStrictnessChange = vi.fn();
  const onStartDrill = vi.fn();

  return {
    openingFamilies: [
      {
        family_name: "Sicilian",
        roots: [
          {
            // 4-field opening_key (matches backend normalize_fen output).
            opening_key: "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -",
            opening_name: "Sicilian Defense",
            opening_family: "Sicilian",
            eco: "B20",
            depth: 1,
          },
        ],
      },
      {
        family_name: "French",
        roots: [
          {
            opening_key: "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -",
            opening_name: "French Defense",
            opening_family: "French",
            eco: "C00",
            depth: 1,
          },
        ],
      },
    ],
    selectedOpening: null,
    playerColor: "white" as const,
    engineElo: 1000,
    strictnessCp: 25,
    maiaEloBins: [800, 1000, 1200] as const,
    botLabel: "Ghost Master 1000",
    isLoadingOpenings: false,
    isStarting: false,
    startError: null,
    onSelectOpening,
    onPlayerColorChange,
    onEngineEloChange,
    onStrictnessChange,
    onStartDrill,
  };
};

describe("DrillSetupPanel", () => {
  it("renders left-side labels for each field", () => {
    render(<DrillSetupPanel {...makeProps()} />);
    expect(screen.getByText("Opening")).toBeInTheDocument();
    expect(screen.getByText("Side")).toBeInTheDocument();
    expect(screen.getByText("Engine Difficulty")).toBeInTheDocument();
    expect(screen.getByText("Strictness")).toBeInTheDocument();
  });

  it("does not render a Random side option", () => {
    render(<DrillSetupPanel {...makeProps()} />);
    expect(screen.queryByRole("button", { name: /^random$/i })).not.toBeInTheDocument();
  });

  it("does not render the strictness tolerance hint", () => {
    render(<DrillSetupPanel {...makeProps()} />);
    expect(
      screen.queryByText(/Adjust tolerance for acceptable moves/i),
    ).not.toBeInTheDocument();
  });

  it("opens the opening picker and selects an opening", () => {
    const props = makeProps();
    render(<DrillSetupPanel {...props} />);

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: /Sicilian Defense/ }));

    expect(props.onSelectOpening).toHaveBeenCalledWith(
      expect.objectContaining({
        opening_key: props.openingFamilies![0].roots[0].opening_key,
      }),
    );
  });

  it("calls onPlayerColorChange when a color toggle is clicked", () => {
    const props = makeProps();
    render(<DrillSetupPanel {...props} />);

    fireEvent.click(screen.getByRole("button", { name: /^white$/i }));
    expect(props.onPlayerColorChange).toHaveBeenCalledWith("white");

    fireEvent.click(screen.getByRole("button", { name: /^black$/i }));
    expect(props.onPlayerColorChange).toHaveBeenCalledWith("black");
  });

  it("calls onStrictnessChange when slider changes", () => {
    const props = makeProps();
    render(<DrillSetupPanel {...props} />);

    const sliders = screen.getAllByRole("slider");
    const strictnessSlider = sliders.find(
      (s) => s.getAttribute("max") === "50",
    )!;

    fireEvent.change(strictnessSlider, { target: { value: "10" } });
    expect(props.onStrictnessChange).toHaveBeenCalledWith(10);
  });

  it("calls onStartDrill when start button is clicked", () => {
    const props = makeProps();
    render(
      <DrillSetupPanel
        {...props}
        selectedOpening={props.openingFamilies![0].roots[0]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /start drill/i }));
    expect(props.onStartDrill).toHaveBeenCalledTimes(1);
  });

  it("disables start button when no opening is selected", () => {
    const props = makeProps();
    render(<DrillSetupPanel {...props} selectedOpening={null} />);

    expect(screen.getByRole("button", { name: /start drill/i })).toBeDisabled();
  });

  it("disables start button when isStarting is true", () => {
    const props = makeProps();
    render(
      <DrillSetupPanel
        {...props}
        selectedOpening={props.openingFamilies![0].roots[0]}
        isStarting
      />,
    );

    expect(screen.getByRole("button", { name: /starting/i })).toBeDisabled();
  });

  it("renders startError when provided", () => {
    const props = makeProps();
    render(<DrillSetupPanel {...props} startError="Failed to start" />);

    expect(screen.getByText("Failed to start")).toBeInTheDocument();
  });

  it("shows loading state on the picker trigger", () => {
    const props = makeProps();
    render(<DrillSetupPanel {...props} openingFamilies={null} isLoadingOpenings />);

    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.getByText(/loading openings/i)).toBeInTheDocument();
  });
});
