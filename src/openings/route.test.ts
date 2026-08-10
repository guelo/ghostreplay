import { describe, expect, it } from "vitest";
import {
  buildCanonicalReplacement,
  buildOpeningsSearchParams,
  parseOpeningsSearchParams,
} from "./route";

// A normalized 4-field FEN — the same kind of value `opening_key` carries
// (opening_roots.py: "normalized 4-field FEN (durable identity)"), so legacy
// `opening=<fen>` deep links and existing `opening_key` links round-trip here.
const FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -";

describe("buildOpeningsSearchParams", () => {
  it("always emits color first (white and black)", () => {
    expect(buildOpeningsSearchParams({ playerColor: "white" }).toString()).toBe(
      "color=white",
    );
    expect(buildOpeningsSearchParams({ playerColor: "black" }).toString()).toBe(
      "color=black",
    );
  });

  it("emits repeated move= in order for a tree line", () => {
    const params = buildOpeningsSearchParams({
      playerColor: "white",
      moves: ["e2e4", "c7c5", "g1f3"],
    });
    expect(params.toString()).toBe("color=white&move=e2e4&move=c7c5&move=g1f3");
    expect(params.getAll("move")).toEqual(["e2e4", "c7c5", "g1f3"]);
  });

  it("gives moves precedence — opening is ignored when moves is set", () => {
    const params = buildOpeningsSearchParams({
      playerColor: "white",
      moves: ["e2e4"],
      opening: FEN,
    });
    expect(params.toString()).toBe("color=white&move=e2e4");
    expect(params.get("opening")).toBeNull();
  });

  it("emits color + opening= for a legacy FEN deep-link entry", () => {
    const params = buildOpeningsSearchParams({
      playerColor: "black",
      opening: FEN,
    });
    expect(params.get("opening")).toBe(FEN);
    expect(params.getAll("move")).toEqual([]);
    expect(params.getAll("path")).toEqual([]);
  });

  it("round-trips a FEN-shaped opening value through URLSearchParams encoding", () => {
    const params = buildOpeningsSearchParams({
      playerColor: "white",
      opening: FEN,
    });
    // Spaces/slashes are percent/plus-encoded in the wire form...
    expect(params.toString()).toContain("opening=");
    // ...but get() decodes back to the original FEN.
    expect(params.get("opening")).toBe(FEN);
  });
});

describe("parseOpeningsSearchParams", () => {
  it("accepts only exact white and black color selections", () => {
    expect(
      parseOpeningsSearchParams(new URLSearchParams("color=white")).playerColor,
    ).toBe("white");
    expect(
      parseOpeningsSearchParams(new URLSearchParams("color=black")).playerColor,
    ).toBe("black");
  });

  it("leaves missing, empty, and invalid colors unselected", () => {
    expect(
      parseOpeningsSearchParams(new URLSearchParams("")).playerColor,
    ).toBeNull();
    expect(
      parseOpeningsSearchParams(new URLSearchParams("color=")).playerColor,
    ).toBeNull();
    expect(
      parseOpeningsSearchParams(new URLSearchParams("color=chartreuse"))
        .playerColor,
    ).toBeNull();
    expect(
      parseOpeningsSearchParams(
        new URLSearchParams("color=chartreuse&color=black"),
      ).playerColor,
    ).toBeNull();
  });

  it("returns all repeated move= values in order; absent → []", () => {
    expect(
      parseOpeningsSearchParams(
        new URLSearchParams("color=white&move=e2e4&move=c7c5"),
      ).moves,
    ).toEqual(["e2e4", "c7c5"]);
    expect(
      parseOpeningsSearchParams(new URLSearchParams("color=white")).moves,
    ).toEqual([]);
  });

  it("surfaces the legacy opening entry; absent → null", () => {
    expect(
      parseOpeningsSearchParams(
        new URLSearchParams(
          `color=black&opening=${encodeURIComponent(FEN)}`,
        ),
      ).opening,
    ).toBe(FEN);
    expect(
      parseOpeningsSearchParams(new URLSearchParams("color=white")).opening,
    ).toBeNull();
  });

  it("preserves move and legacy opening inputs while color is unselected", () => {
    const parsed = parseOpeningsSearchParams(
      new URLSearchParams(
        `move=e2e4&move=c7c5&opening=${encodeURIComponent(FEN)}`,
      ),
    );

    expect(parsed).toEqual({
      playerColor: null,
      moves: ["e2e4", "c7c5"],
      opening: FEN,
    });
  });
});

describe("buildCanonicalReplacement", () => {
  it("returns null when the move line is already canonical", () => {
    const current = new URLSearchParams("color=white&move=e2e4");
    expect(buildCanonicalReplacement(current, "white", ["e2e4"])).toBeNull();
  });

  it("returns the new move= params for a truncated canonical line", () => {
    const current = new URLSearchParams("color=white&move=e2e4&move=c7c5");
    const replacement = buildCanonicalReplacement(current, "white", ["e2e4"]);
    expect(replacement?.toString()).toBe("color=white&move=e2e4");
  });

  it("rewrites a legacy opening=<fen> URL to the resolved move= line", () => {
    const current = new URLSearchParams(
      `color=white&opening=${encodeURIComponent(FEN)}`,
    );
    const replacement = buildCanonicalReplacement(current, "white", [
      "e2e4",
      "e7e6",
    ]);
    expect(replacement?.toString()).toBe("color=white&move=e2e4&move=e7e6");
    expect(replacement?.get("opening")).toBeNull();
  });

  it("strips stale opening/path/extra params even when the line is canonical", () => {
    const current = new URLSearchParams(
      "color=white&move=e2e4&opening=foo&path=bar",
    );
    const replacement = buildCanonicalReplacement(current, "white", ["e2e4"]);
    expect(replacement?.toString()).toBe("color=white&move=e2e4");
  });

  it("returns null for an empty canonical line against a bare color URL", () => {
    const current = new URLSearchParams("color=white");
    expect(buildCanonicalReplacement(current, "white", [])).toBeNull();
  });

  it("canonicalizes param order and strips unknown params (review finding 3)", () => {
    // Reordered params: move before color.
    expect(
      buildCanonicalReplacement(
        new URLSearchParams("move=e2e4&color=white"),
        "white",
        ["e2e4"],
      )?.toString(),
    ).toBe("color=white&move=e2e4");
    // Unknown noise param stripped.
    expect(
      buildCanonicalReplacement(
        new URLSearchParams("color=white&move=e2e4&foo=bar"),
        "white",
        ["e2e4"],
      )?.toString(),
    ).toBe("color=white&move=e2e4");
  });
});

describe("round-trip", () => {
  it("parse(build({playerColor, moves})) preserves playerColor and moves", () => {
    const route = { playerColor: "black" as const, moves: ["e2e4", "c7c5"] };
    const parsed = parseOpeningsSearchParams(buildOpeningsSearchParams(route));
    expect(parsed.playerColor).toBe("black");
    expect(parsed.moves).toEqual(["e2e4", "c7c5"]);
    expect(parsed.opening).toBeNull();
  });
});
