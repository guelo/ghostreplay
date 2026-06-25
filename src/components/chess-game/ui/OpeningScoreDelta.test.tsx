import { describe, expect, it } from "vitest";
import { render, screen } from "../../../test/utils";
import type { OpeningScoreDeltaItem } from "../../../utils/api";
import OpeningScoreDelta from "./OpeningScoreDelta";

const item = (over: Partial<OpeningScoreDeltaItem>): OpeningScoreDeltaItem => ({
  opening_key: "k1",
  opening_name: "Italian Game",
  opening_family: "Italian Game",
  eco: "C50",
  depth: 3,
  before: 41,
  after: 44,
  delta: 3,
  is_new: false,
  ...over,
});

describe("OpeningScoreDelta", () => {
  it("renders nothing for null or empty changes", () => {
    const { container: a } = render(<OpeningScoreDelta changes={null} />);
    expect(a).toBeEmptyDOMElement();
    const { container: b } = render(<OpeningScoreDelta changes={[]} />);
    expect(b).toBeEmptyDOMElement();
  });

  it("renders a before -> after numeric delta with grade", () => {
    render(<OpeningScoreDelta changes={[item({})]} />);
    expect(screen.getByText("Italian Game")).toBeInTheDocument();
    expect(screen.getByText("41")).toBeInTheDocument();
    expect(screen.getByText("44")).toBeInTheDocument();
    expect(screen.getByText("(+3)")).toBeInTheDocument();
    // after=44 grades B (>=38).
    expect(screen.getByLabelText("Grade B")).toBeInTheDocument();
  });

  it("renders a negative delta with the down tone", () => {
    const { container } = render(
      <OpeningScoreDelta changes={[item({ before: 50, after: 44, delta: -6 })]} />,
    );
    expect(screen.getByText("(-6)")).toBeInTheDocument();
    expect(
      container.querySelector(".opening-score-delta__row--down"),
    ).not.toBeNull();
  });

  it("renders 'new' instead of a numeric delta for a first-time opening", () => {
    render(
      <OpeningScoreDelta
        changes={[item({ before: null, delta: null, is_new: true, after: 30 })]}
      />,
    );
    expect(screen.getByText(/new/)).toBeInTheDocument();
    // No arrow / before value for a brand-new opening.
    expect(screen.queryByText("→")).not.toBeInTheDocument();
  });

  it("omits the grade tag when the after-score is unknown", () => {
    render(
      <OpeningScoreDelta
        changes={[item({ before: null, after: null, delta: null, is_new: true })]}
      />,
    );
    expect(screen.queryByLabelText(/^Grade /)).not.toBeInTheDocument();
  });

  it("renders one row per played opening, broadest -> deepest", () => {
    render(
      <OpeningScoreDelta
        changes={[
          item({ opening_key: "a", opening_name: "King's Pawn Game" }),
          item({ opening_key: "b", opening_name: "Ruy Lopez" }),
        ]}
      />,
    );
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("King's Pawn Game");
    expect(rows[1]).toHaveTextContent("Ruy Lopez");
  });
});
