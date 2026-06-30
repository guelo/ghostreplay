import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OpeningTreeNodeCard, {
  type OpeningTreeNodeView,
} from "./OpeningTreeNodeCard";

// A scored, non-root child node (White's 2nd move, Ruy Lopez).
const childView: OpeningTreeNodeView = {
  ply: 3,
  san: "Nf3",
  openingName: "Ruy Lopez",
  eco: "C60",
  inBook: true,
  isUserSelected: false,
  score: 72,
  evalCp: 120,
  evalMate: null,
  coverage: 0.5,
  gameCount: 1234,
  confidence: 0.8,
  isTerminal: false,
  terminalReason: null,
  drillOpeningKey: null,
};

// The synthesized root: no SAN/name/score, eval from root_eval only.
const rootView: OpeningTreeNodeView = {
  ply: 0,
  san: null,
  openingName: null,
  eco: null,
  inBook: true,
  isUserSelected: false,
  score: null,
  evalCp: 40,
  evalMate: null,
  coverage: null,
  gameCount: null,
  confidence: null,
  isTerminal: false,
  terminalReason: null,
  drillOpeningKey: null,
};

describe("OpeningTreeNodeCard — compact", () => {
  it("renders a selection button that fires onSelect and reflects isSelected", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const { rerender } = render(
      <OpeningTreeNodeCard
        variant="compact"
        node={childView}
        onSelect={onSelect}
        isSelected={false}
      />,
    );

    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("aria-pressed", "false");

    await user.click(button);
    expect(onSelect).toHaveBeenCalledTimes(1);

    rerender(
      <OpeningTreeNodeCard
        variant="compact"
        node={childView}
        onSelect={onSelect}
        isSelected
      />,
    );
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "true");
  });

  it("shows SAN, score, grade, full opening name, and eval", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={childView}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Nf3")).toBeInTheDocument();
    expect(screen.getByText("72")).toBeInTheDocument();

    const grade = screen.getByLabelText("Grade A");
    expect(grade).toHaveTextContent("A");

    const name = screen.getByText("Ruy Lopez");
    expect(name).toHaveAttribute("title", "Ruy Lopez");

    expect(screen.getByText("+1.2")).toBeInTheDocument();
  });

  it("dashes a null eval and a null score (with a 'No data' grade)", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, score: null, evalCp: null, evalMate: null }}
        onSelect={vi.fn()}
      />,
    );

    // Score and grade tag both dash; the grade tag carries the accessible name.
    const grade = screen.getByLabelText("No data");
    expect(grade).toHaveTextContent("—");
    // Score "—" + eval "—" + grade "—" all render.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("falls back to 'Unclassified' for a non-root null name", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, openingName: null }}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Unclassified")).toBeInTheDocument();
  });

  it("nests no further interactive controls inside the selection button", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={childView}
        onSelect={vi.fn()}
      />,
    );

    const button = screen.getByRole("button");
    expect(within(button).queryByRole("button")).toBeNull();
    expect(within(button).queryByRole("link")).toBeNull();
  });

  it("sets disclosure attributes only when those props are provided", () => {
    const { rerender } = render(
      <OpeningTreeNodeCard
        variant="compact"
        node={childView}
        onSelect={vi.fn()}
      />,
    );
    const plain = screen.getByRole("button");
    expect(plain).not.toHaveAttribute("aria-expanded");
    expect(plain).not.toHaveAttribute("aria-controls");

    rerender(
      <OpeningTreeNodeCard
        variant="compact"
        node={childView}
        onSelect={vi.fn()}
        isExpanded={false}
        controlsId="panel-1"
      />,
    );
    const disclosure = screen.getByRole("button");
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    expect(disclosure).toHaveAttribute("aria-controls", "panel-1");
  });

  it("renders a static (non-button) card when no onSelect is given", () => {
    render(<OpeningTreeNodeCard variant="compact" node={childView} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Nf3")).toBeInTheDocument();
  });

  it("shows an 'Off book' chip for off-book moves and hides it for book moves", () => {
    const { rerender } = render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, inBook: false }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Off book")).toBeInTheDocument();

    rerender(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, inBook: true }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByText("Off book")).toBeNull();
  });

  it("opens the 'Off book' info popover on click without selecting the card", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, inBook: false }}
        onSelect={onSelect}
      />,
    );

    expect(screen.queryByRole("tooltip")).toBeNull();
    await user.click(
      screen.getByRole("button", { name: /off book — what does this mean/i }),
    );

    // The popover appears, and the chip click did NOT bubble to card selection.
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    expect(onSelect).not.toHaveBeenCalled();

    // Escape dismisses it.
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("shows a 'Your move' chip for user-selected moves instead of 'Off book'", () => {
    const { rerender } = render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, inBook: false, isUserSelected: true }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Your move")).toBeInTheDocument();
    // The third type wins over off-book — no "Off book" chip on the same card.
    expect(screen.queryByText("Off book")).toBeNull();

    // A user-selected move that crossed a book boundary (inBook=true) still gets
    // the "Your move" chip (and a book move would show no chip).
    rerender(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, inBook: true, isUserSelected: true }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Your move")).toBeInTheDocument();
  });

  it("opens the 'Your move' info popover on click without selecting the card", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, isUserSelected: true }}
        onSelect={onSelect}
      />,
    );

    expect(screen.queryByRole("tooltip")).toBeNull();
    await user.click(
      screen.getByRole("button", { name: /your move — what does this mean/i }),
    );
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("never shows the 'Off book' chip on the root", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...rootView, inBook: false }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByText("Off book")).toBeNull();
  });

  it("renders the root as 'Start' with eval but no name slot", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={rootView}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Start")).toBeInTheDocument();
    expect(screen.getByText("+0.4")).toBeInTheDocument();
    // No name fallback at the root — the name slot is omitted entirely.
    expect(screen.queryByText("Unclassified")).toBeNull();
    // Score dashes; grade tag reports "No data".
    expect(screen.getByLabelText("No data")).toHaveTextContent("—");
  });
});

describe("OpeningTreeNodeCard — expanded", () => {
  it("is not a button and renders the move label, score+grade, eval, and metrics", () => {
    render(<OpeningTreeNodeCard variant="expanded" node={childView} />);

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("2. Nf3")).toBeInTheDocument();
    expect(screen.getByText("72")).toBeInTheDocument();
    expect(screen.getByLabelText("Grade A")).toHaveTextContent("A");
    expect(screen.getByText("+1.2")).toBeInTheDocument();

    expect(screen.getByText("Coverage")).toBeInTheDocument();
    expect(screen.getByText("Confidence")).toBeInTheDocument();
    // Games comes from gameCount, localized.
    expect(screen.getByText("Games")).toBeInTheDocument();
    expect(screen.getByText((1234).toLocaleString())).toBeInTheDocument();

    // No embedded chessboard.
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("renders one Start Drill button that fires onStartDrill when drillable", async () => {
    const user = userEvent.setup();
    const onStartDrill = vi.fn();
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, drillOpeningKey: "ruy-key" }}
        onStartDrill={onStartDrill}
      />,
    );

    const drill = screen.getByRole("button", { name: "Start Drill" });
    await user.click(drill);
    expect(onStartDrill).toHaveBeenCalledTimes(1);
  });

  it("shows Start Drill whenever a handler is provided, even without a drill key", () => {
    // drillOpeningKey no longer gates the button — every drillable card passes a
    // handler (the page wires it for move cards, omits it for the root).
    const { rerender } = render(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, drillOpeningKey: null }}
        onStartDrill={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Start Drill" }),
    ).toBeInTheDocument();

    // No handler (e.g. the synthesized root) → no button.
    rerender(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, drillOpeningKey: "ruy-key" }}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Start Drill" }),
    ).toBeNull();
  });

  it("renders a terminal note for terminal nodes", () => {
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, isTerminal: true, terminalReason: "checkmate" }}
      />,
    );
    expect(screen.getByText("Checkmate")).toBeInTheDocument();
  });

  it("shows the 'Off book' chip next to the name for off-book expanded moves", () => {
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, inBook: false }}
      />,
    );
    expect(screen.getByText("Off book")).toBeInTheDocument();
  });

  it("shows the 'Your move' chip next to the name for user-selected expanded moves", () => {
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, inBook: false, isUserSelected: true }}
      />,
    );
    expect(screen.getByText("Your move")).toBeInTheDocument();
    expect(screen.queryByText("Off book")).toBeNull();
  });

  it("renders the root with a 'Starting position' header, no name line, dashed metrics, and eval", () => {
    render(<OpeningTreeNodeCard variant="expanded" node={rootView} />);

    expect(screen.getByText("Starting position")).toBeInTheDocument();
    expect(screen.queryByText("Unclassified")).toBeNull();
    expect(screen.getByText("+0.4")).toBeInTheDocument();
    // Coverage/Games/Confidence all dash on the root.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  it.each<[number | null, string]>([
    [90, "Grade A"],
    [42, "Grade B"],
    [32, "Grade C"],
    [24, "Grade D"],
    [10, "Grade F"],
    [null, "No data"],
  ])("exposes the grade accessible name for score %s", (score, label) => {
    render(
      <OpeningTreeNodeCard variant="expanded" node={{ ...childView, score }} />,
    );
    expect(screen.getByLabelText(label)).toBeInTheDocument();
  });
});

// An opening-lineage family: position-identified, so no SAN/ply/eval. inBook is
// always true and isUserSelected false, so the move-type chips never apply.
const familyView: OpeningTreeNodeView = {
  ply: 2,
  san: null,
  openingName: "Ruy Lopez",
  eco: "C60",
  inBook: true,
  isUserSelected: false,
  score: 72,
  evalCp: null,
  evalMate: null,
  coverage: 0.5,
  gameCount: 1234,
  confidence: 0.8,
  isTerminal: false,
  terminalReason: null,
  drillOpeningKey: "ruy-key",
};

describe("OpeningTreeNodeCard — family mode", () => {
  it("compact: shows the name as the primary line (not Start/SAN) with score, grade, and ECO", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        kind="family"
        node={familyView}
        onSelect={vi.fn()}
        ariaLabel="Select Ruy Lopez and toggle details"
      />,
    );

    // Name is the primary line; no "Start"/SAN.
    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(screen.queryByText("Start")).toBeNull();
    // Score + grade ride alongside.
    expect(screen.getByText("72")).toBeInTheDocument();
    expect(screen.getByLabelText("Grade A")).toHaveTextContent("A");
    // ECO is the muted secondary line.
    expect(screen.getByText("C60")).toBeInTheDocument();
    // The supplied ariaLabel becomes the button's accessible name.
    expect(
      screen.getByRole("button", { name: "Select Ruy Lopez and toggle details" }),
    ).toBeInTheDocument();
  });

  it("compact: never shows move-type chips or an eval, even when inBook is false", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        kind="family"
        node={{ ...familyView, inBook: false, isUserSelected: true }}
        onSelect={vi.fn()}
      />,
    );

    // Family mode has no off-book / your-move concept and no eval slot.
    expect(screen.queryByText("Off book")).toBeNull();
    expect(screen.queryByText("Your move")).toBeNull();
    expect(screen.queryByText("+1.2")).toBeNull();
  });

  it("compact: renders single-line when there is no ECO", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        kind="family"
        node={{ ...familyView, eco: null }}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(screen.queryByText("C60")).toBeNull();
  });

  it("expanded: name header (no move label), no Eval tile, the three metrics", () => {
    render(
      <OpeningTreeNodeCard variant="expanded" kind="family" node={familyView} />,
    );

    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(screen.queryByText("Starting position")).toBeNull();
    // The Eval tile is dropped in family mode.
    expect(screen.queryByText("Eval")).toBeNull();
    // The Score tile + the three metrics remain.
    expect(screen.getByText("Score")).toBeInTheDocument();
    expect(screen.getByLabelText("Grade A")).toHaveTextContent("A");
    expect(screen.getByText("Coverage")).toBeInTheDocument();
    expect(screen.getByText("Games")).toBeInTheDocument();
    expect(screen.getByText((1234).toLocaleString())).toBeInTheDocument();
    expect(screen.getByText("Confidence")).toBeInTheDocument();
  });

  it("expanded: a collapse surface fires onCollapse while Start Drill still fires", async () => {
    const user = userEvent.setup();
    const onCollapse = vi.fn();
    const onStartDrill = vi.fn();
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        kind="family"
        node={familyView}
        onCollapse={onCollapse}
        onStartDrill={onStartDrill}
      />,
    );

    // Start Drill fires its own handler and does NOT collapse the card.
    await user.click(screen.getByRole("button", { name: "Start Drill" }));
    expect(onStartDrill).toHaveBeenCalledTimes(1);
    expect(onCollapse).not.toHaveBeenCalled();

    // The full-surface collapse overlay fires onCollapse.
    await user.click(
      screen.getByRole("button", { name: /Collapse Ruy Lopez details/ }),
    );
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });
});
