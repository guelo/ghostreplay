import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../../test/utils";
import DrillSetupPanel from "./DrillSetupPanel";

const makeProps = () => {
  const onSelectOpening = vi.fn();
  const onPlayerColorChange = vi.fn();
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
    selectedLine: null as string[] | null,
    playerColor: "white" as const,
    strictnessCp: 25 as number | null,
    isLoadingOpenings: false,
    isStarting: false,
    startError: null,
    onSelectOpening,
    onPlayerColorChange,
    onStrictnessChange,
    onStartDrill,
  };
};

describe("DrillSetupPanel", () => {
  it("renders the drill fields without a manual difficulty control", () => {
    render(<DrillSetupPanel {...makeProps()} />);
    expect(screen.getByText("Opening")).toBeInTheDocument();
    expect(screen.getByText("Side")).toBeInTheDocument();
    expect(screen.getByText("Strictness")).toBeInTheDocument();
    expect(screen.queryByText("Engine Difficulty")).not.toBeInTheDocument();
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
        opening: expect.objectContaining({
          opening_key: props.openingFamilies![0].roots[0].opening_key,
        }),
        line: null,
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

  it("renders three strictness tier buttons", () => {
    render(<DrillSetupPanel {...makeProps()} />);
    expect(screen.getByRole("button", { name: /^strict$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^standard$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^lenient$/i })).toBeInTheDocument();
  });

  it("starts unset: no active tier, no fine-tune slider, Start disabled despite an opening", () => {
    const props = makeProps();
    render(
      <DrillSetupPanel
        {...props}
        strictnessCp={null}
        selectedOpening={props.openingFamilies![0].roots[0]}
      />,
    );

    for (const name of [/^strict$/i, /^standard$/i, /^lenient$/i]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    }
    expect(
      screen.queryByRole("slider", { name: /fine-tune strictness/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    expect(
      screen.getByText(/pick a strictness to start/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start drill/i })).toBeDisabled();
  });

  it("clicking a tier seeds its representative cp", () => {
    const props = makeProps();
    render(<DrillSetupPanel {...props} strictnessCp={null} />);

    fireEvent.click(screen.getByRole("button", { name: /^strict$/i }));
    // Strict seeds 0 (exact-best semantics), not merely a low cp.
    expect(props.onStrictnessChange).toHaveBeenCalledWith(0);

    fireEvent.click(screen.getByRole("button", { name: /^standard$/i }));
    expect(props.onStrictnessChange).toHaveBeenCalledWith(25);

    fireEvent.click(screen.getByRole("button", { name: /^lenient$/i }));
    expect(props.onStrictnessChange).toHaveBeenCalledWith(50);
  });

  it("marks the tier containing the current cp as active", () => {
    const props = makeProps();
    const { rerender } = render(<DrillSetupPanel {...props} strictnessCp={0} />);
    expect(screen.getByRole("button", { name: /^strict$/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    rerender(<DrillSetupPanel {...props} strictnessCp={30} />);
    expect(screen.getByRole("button", { name: /^standard$/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: /^strict$/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("calls onStrictnessChange when the fine-tune slider changes within the tier band", () => {
    const props = makeProps();
    render(<DrillSetupPanel {...props} />);

    // strictnessCp=25 → Standard band, so the slider is clamped to 16–35.
    const slider = screen.getByRole("slider", { name: /fine-tune strictness/i });
    expect(slider).toHaveAttribute("min", "16");
    expect(slider).toHaveAttribute("max", "35");

    fireEvent.change(slider, { target: { value: "30" } });
    expect(props.onStrictnessChange).toHaveBeenCalledWith(30);
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

  // Reachability contract, not styling: .drill-setup__fields is the element
  // that scrolls when the panel is height-capped. Start Drill living inside it
  // is what put the CTA below the fold under 768px (g-drill-cta-clip).
  it("keeps the start button outside the scrollable fields region", () => {
    const props = makeProps();
    const { container } = render(
      <DrillSetupPanel
        {...props}
        selectedOpening={props.openingFamilies![0].roots[0]}
      />,
    );

    const fields = container.querySelector(".drill-setup__fields");
    const startButton = screen.getByRole("button", { name: /start drill/i });
    expect(fields).not.toBeNull();
    expect(fields!.contains(startButton)).toBe(false);
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
