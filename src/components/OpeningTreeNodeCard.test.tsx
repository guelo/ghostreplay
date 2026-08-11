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
  isTransposition: false,
  score: 72,
  evalCp: 120,
  evalMate: null,
  coverage: 0.5,
  gameCount: 1234,
  isTerminal: false,
  terminalReason: null,
  drillOpeningKey: null,
  moveListSan: ["e4", "e5", "Nf3"],
  moveListStartPly: 1,
};

// The synthesized root: no SAN/name/score, eval from root_eval only.
const rootView: OpeningTreeNodeView = {
  ply: 0,
  san: null,
  openingName: null,
  eco: null,
  inBook: true,
  isUserSelected: false,
  isTransposition: false,
  score: null,
  evalCp: 40,
  evalMate: null,
  coverage: null,
  gameCount: null,
  isTerminal: false,
  terminalReason: null,
  drillOpeningKey: null,
  moveListSan: [],
  moveListStartPly: 1,
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

  it("leads with the opening name, then score, grade, move list (last bold), and eval", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={childView}
        onSelect={vi.fn()}
      />,
    );

    // The name is the primary (top) label, truncating as the lead.
    const name = screen.getByText("Ruy Lopez");
    expect(name).toHaveClass("tree-node-card__move--name");
    expect(name).toHaveAttribute("title", "Ruy Lopez");

    expect(screen.getByText("72")).toBeInTheDocument();
    expect(screen.getByLabelText("Grade A")).toHaveTextContent("A");

    // The played move list is the secondary line; the last move is bold.
    expect(screen.getByText("1.e4")).toBeInTheDocument();
    const last = screen.getByText("2.Nf3");
    expect(last.tagName).toBe("STRONG");

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

  it("renders a hard zero score as data, not as no-data", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, score: 0 }}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("0")).toBeInTheDocument();
    expect(screen.getByLabelText("Grade F")).toHaveTextContent("F");
    expect(screen.queryByLabelText("No data")).toBeNull();
  });

  it("keeps an ordinary fractional score integer-formatted", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, score: 72.4 }}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("72")).toBeInTheDocument();
    expect(screen.queryByText("72.4")).toBeNull();
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
    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
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

  it("shows 'Transposition' instead of 'Off book' for an overlay edge", () => {
    // A transposition edge is not in this parent's book (inBook=false) but IS in
    // the book through another move order — labelling it "Off book" would claim
    // it came from the player's own games (g-openings-transpose).
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, inBook: false, isTransposition: true }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Transposition")).toBeInTheDocument();
    expect(screen.queryByText("Off book")).toBeNull();
  });

  it("prefers 'Your move' over 'Transposition' when both flags are set", () => {
    // The documented non-disjoint case: a selected overlay edge outside the
    // navigable set. Exactly one move-type chip renders, and "Your move" wins.
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={{ ...childView, inBook: false, isTransposition: true, isUserSelected: true }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Your move")).toBeInTheDocument();
    expect(screen.queryByText("Transposition")).toBeNull();
    expect(screen.queryByText("Off book")).toBeNull();
  });

  it("shows the 'Transposition' chip on the expanded card too", () => {
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, inBook: false, isTransposition: true }}
      />,
    );
    expect(screen.getByText("Transposition")).toBeInTheDocument();
    expect(screen.queryByText("Off book")).toBeNull();
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

  it("renders the synthesized root as 'Starting position' with no secondary line", () => {
    render(
      <OpeningTreeNodeCard
        variant="compact"
        node={rootView}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Starting position")).toBeInTheDocument();
    // No "Unclassified" fallback — the root gets its own label.
    expect(screen.queryByText("Unclassified")).toBeNull();
    // The root has no move list and no secondary line, so the start eval that
    // would ride the secondary line is not shown on the compact root.
    expect(screen.queryByText("+0.4")).toBeNull();
    // Score dashes; grade tag reports "No data".
    expect(screen.getByLabelText("No data")).toHaveTextContent("—");
  });
});

describe("OpeningTreeNodeCard — expanded", () => {
  it("keeps an ordinary fractional score integer-formatted", () => {
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, score: 72.4 }}
      />,
    );

    expect(screen.getByText("72")).toBeInTheDocument();
    expect(screen.queryByText("72.4")).toBeNull();
  });

  it("is not a button and renders the name header, move list, score+grade, eval, and metrics", () => {
    render(<OpeningTreeNodeCard variant="expanded" node={childView} />);

    expect(screen.queryByRole("button")).toBeNull();
    // Header leads with the opening name (not the move label).
    const header = screen.getByText("Ruy Lopez");
    expect(header).toHaveClass("tree-node-card__move-label");
    // The played move list renders under the header, last move bold.
    expect(screen.getByText("1.e4")).toBeInTheDocument();
    expect(screen.getByText("2.Nf3").tagName).toBe("STRONG");
    expect(screen.getByText("72")).toBeInTheDocument();
    expect(screen.getByLabelText("Grade A")).toHaveTextContent("A");
    expect(screen.getByText("+1.2")).toBeInTheDocument();

    expect(screen.getByText("Coverage")).toBeInTheDocument();
    // Games comes from gameCount, localized.
    expect(screen.getByText("Games")).toBeInTheDocument();
    expect(screen.getByText((1234).toLocaleString())).toBeInTheDocument();

    // No embedded chessboard.
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("renders caller-owned footer controls without assigning their semantics", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, drillOpeningKey: "ruy-key" }}
        footerAction={
          <button type="button" onClick={onAction}>
            Use this opening
          </button>
        }
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Use this opening" }),
    );
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it("does not infer an action from drillOpeningKey", () => {
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        node={{ ...childView, drillOpeningKey: "ruy-key" }}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
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
    // Coverage/Games both dash on the root.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it.each<[number | null, string]>([
    [90, "Grade A"],
    [42, "Grade B"],
    [20, "Grade C"],
    [4, "Grade D"],
    [1, "Grade F"],
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
  isTransposition: false,
  score: 72,
  evalCp: null,
  evalMate: null,
  coverage: 0.5,
  gameCount: 1234,
  isTerminal: false,
  terminalReason: null,
  drillOpeningKey: "ruy-key",
  moveListSan: ["e4", "e5", "Nf3", "Nc6", "Bb5"],
  moveListStartPly: 1,
};

describe("OpeningTreeNodeCard — family mode", () => {
  it("compact: shows the name as the primary line with score, grade, and the played move list", () => {
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
    // The played move list is the secondary line (not the ECO), last move bold.
    expect(screen.getByText("1.e4")).toBeInTheDocument();
    expect(screen.getByText("3.Bb5").tagName).toBe("STRONG");
    expect(screen.queryByText("C60")).toBeNull();
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

  it("compact: a family with no moves shows only the name — never 'Starting position'", () => {
    // A family card's san is also null, but it is NOT the synthesized /openings
    // root, so it must never read as "Starting position"; with no moves it just
    // renders the name and no secondary line.
    render(
      <OpeningTreeNodeCard
        variant="compact"
        kind="family"
        node={{ ...familyView, moveListSan: [] }}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(screen.queryByText("Starting position")).toBeNull();
    // No move list rendered (no tokens).
    expect(screen.queryByText("1.e4")).toBeNull();
  });

  it("expanded: name header (no move label), no Eval tile, the two metrics", () => {
    render(
      <OpeningTreeNodeCard variant="expanded" kind="family" node={familyView} />,
    );

    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(screen.queryByText("Starting position")).toBeNull();
    // The Eval tile is dropped in family mode.
    expect(screen.queryByText("Eval")).toBeNull();
    // The Score tile + the two metrics remain.
    expect(screen.getByText("Score")).toBeInTheDocument();
    expect(screen.getByLabelText("Grade A")).toHaveTextContent("A");
    expect(screen.getByText("Coverage")).toBeInTheDocument();
    expect(screen.getByText("Games")).toBeInTheDocument();
    expect(screen.getByText((1234).toLocaleString())).toBeInTheDocument();
    expect(screen.queryByText("Confidence")).toBeNull();
  });

  it("expanded: a collapse surface stays independent from multiple footer controls", async () => {
    const user = userEvent.setup();
    const onCollapse = vi.fn();
    const onStartDrill = vi.fn();
    const onView = vi.fn();
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        kind="family"
        node={familyView}
        onCollapse={onCollapse}
        footerAction={
          <>
            <button type="button" onClick={onStartDrill}>
              Start Drill
            </button>
            <button type="button" onClick={onView}>
              View in Openings
            </button>
          </>
        }
      />,
    );

    await user.click(screen.getByRole("button", { name: "Start Drill" }));
    expect(onStartDrill).toHaveBeenCalledTimes(1);
    expect(onCollapse).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "View in Openings" }));
    expect(onView).toHaveBeenCalledTimes(1);
    expect(onCollapse).not.toHaveBeenCalled();

    // The full-surface collapse overlay fires onCollapse.
    await user.click(
      screen.getByRole("button", { name: /Collapse Ruy Lopez details/ }),
    );
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it("expanded: renders footerAction inside the card; clicking it does not collapse", async () => {
    const user = userEvent.setup();
    const onCollapse = vi.fn();
    const onFooter = vi.fn();
    render(
      <OpeningTreeNodeCard
        variant="expanded"
        kind="family"
        node={familyView}
        onCollapse={onCollapse}
        footerAction={
          <button type="button" onClick={onFooter}>
            View in Openings
          </button>
        }
      />,
    );

    // The footer action lives inside the card (a descendant of the expanded card).
    const card = document.querySelector(".tree-node-card--expanded");
    const footer = screen.getByRole("button", { name: "View in Openings" });
    expect(card).toContainElement(footer);

    // Tapping it fires its own handler and does NOT collapse the card.
    await user.click(footer);
    expect(onFooter).toHaveBeenCalledTimes(1);
    expect(onCollapse).not.toHaveBeenCalled();
  });
});

describe("OpeningTreeNodeCard — move list truncation", () => {
  it("compact truncates the move list to one line; expanded wraps it", () => {
    const { rerender } = render(
      <OpeningTreeNodeCard variant="compact" node={childView} />,
    );
    const compact = screen.getByText("1.e4").closest(".tree-node-card__move-list");
    expect(compact).toHaveClass("tree-node-card__move-list--compact");

    rerender(<OpeningTreeNodeCard variant="expanded" node={childView} />);
    const expanded = screen
      .getByText("1.e4")
      .closest(".tree-node-card__move-list");
    expect(expanded).toHaveClass("tree-node-card__move-list--expanded");
  });
});
