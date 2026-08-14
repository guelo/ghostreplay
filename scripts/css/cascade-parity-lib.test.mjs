import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  assertIndexedEntries,
  buildFixtureDescriptors,
  enrichCssScenariosFromTsx,
  extractCssSelectorScenarios,
  mergeComputedPropertyNames,
} from "./cascade-parity-lib.mjs";

const singleScenario = (selector) => {
  const result = extractCssSelectorScenarios(`${selector} { color: red; }`);
  expect(result.unsupported).toEqual([]);
  expect(result.scenarios).toHaveLength(1);
  return result.scenarios[0];
};

describe("CSS selector fixture synthesis", () => {
  it("builds cross-component descendant ancestry", () => {
    const scenario = singleScenario(".game-page .material-icons");

    expect(scenario.combinators).toEqual([" "]);
    expect(scenario.nodes.map((node) => node.classNames)).toEqual([
      ["game-page"],
      ["material-icons"],
    ]);
  });

  it("places forced state markers on the ancestor that owns the state", () => {
    const scenario = singleScenario(
      ".home-feature:hover .home-feature__icon",
    );

    expect(scenario.nodes[0].classNames).toContain("__cascade-force-hover");
    expect(scenario.nodes[1].classNames).not.toContain(
      "__cascade-force-hover",
    );
    expect(scenario.matchSelector).toBe(
      ".home-feature.__cascade-force-hover .home-feature__icon",
    );
  });

  it("materializes ancestor data attributes", () => {
    const scenario = singleScenario(
      '.perfect-streak-badge[data-fire-intensity="flame"] .perfect-streak-badge__flame',
    );

    expect(scenario.nodes[0].attributes).toEqual({
      "data-fire-intensity": "flame",
    });
  });

  it("preserves child, adjacent, and general sibling combinators", () => {
    const scenario = singleScenario(".a > .b + .c ~ .d");

    expect(scenario.combinators).toEqual([">", "+", "~"]);
    expect(scenario.nodes.map((node) => node.classNames[0])).toEqual([
      "a",
      "b",
      "c",
      "d",
    ]);
  });

  it("materializes simple relational :has() requirements", () => {
    const scenario = singleScenario(
      ".analysis-board:has(.analysis-board__mobile-toolbar)",
    );

    expect(scenario.nodes[0].requiredChildren[0].classNames).toEqual([
      "analysis-board__mobile-toolbar",
    ]);
  });

  it("reports unsupported functional alternatives instead of dropping them", () => {
    const result = extractCssSelectorScenarios(
      ":is(.first, .second) { color: red; }",
    );

    expect(result.scenarios).toEqual([]);
    expect(result.unsupported).toEqual([
      expect.objectContaining({
        selector: ":is(.first,.second)",
        reason: ":is() with multiple alternatives is unsupported",
      }),
    ]);
  });

  it("adds only TSX-proven same-element class invariants", () => {
    const cssScenario = singleScenario(".home-final-cta .home-button");
    const tsxScenario = {
      kind: "tsx",
      source: "src/App.tsx:1",
      nodes: [
        {
          tag: "div",
          classNames: ["home-final-cta"],
          attributes: {},
          requiredChildren: [],
        },
        {
          tag: "a",
          classNames: ["home-button", "chess-button", "primary"],
          attributes: {},
          requiredChildren: [],
        },
      ],
      combinators: [" "],
      pseudoElements: [],
    };

    const [enriched] = enrichCssScenariosFromTsx(
      [cssScenario],
      [tsxScenario],
    );

    expect(enriched.nodes[1].classNames).toEqual([
      "home-button",
      "chess-button",
      "primary",
    ]);
    expect(enriched.tsxEnrichmentMatchCount).toBe(1);
    expect(enriched.tsxEnrichmentMode).toBe("contextual");
  });

  it("retains target invariants when ancestry crosses component files", () => {
    const cssScenario = singleScenario(".game-page .material-icons");
    const tsxScenario = {
      kind: "tsx",
      source: "src/components/Icon.tsx:1",
      nodes: [
        {
          tag: "span",
          classNames: ["material-icons", "game-status-icon"],
          attributes: {},
          requiredChildren: [],
        },
      ],
      combinators: [],
      pseudoElements: [],
    };

    const [enriched] = enrichCssScenariosFromTsx(
      [cssScenario],
      [tsxScenario],
    );

    expect(enriched.nodes[0].classNames).toEqual(["game-page"]);
    expect(enriched.nodes[1].classNames).toEqual([
      "material-icons",
      "game-status-icon",
    ]);
    expect(enriched.tsxEnrichmentMode).toBe("target-only");
  });

  it("keeps exact selector state while checking TSX base and combined states", () => {
    const selectorScenario = singleScenario(".game-page .material-icons");
    const tsxScenario = {
      kind: "tsx",
      source: "src/example.tsx:1",
      nodes: [
        {
          tag: "span",
          classNames: ["material-icons"],
          attributes: {},
          requiredChildren: [],
        },
      ],
      combinators: [],
      pseudoElements: [],
    };

    const result = buildFixtureDescriptors([tsxScenario, selectorScenario]);

    expect(result.fixtures).toHaveLength(3);
    expect(result.targets[0].states).toEqual([]);
    expect(result.targets[1].states).toEqual(
      expect.arrayContaining([
        "__cascade-force-hover",
        "__cascade-force-focus-visible",
      ]),
    );
    expect(result.targets.at(-1).selector).toBe(
      ".game-page .material-icons",
    );
  });
});

describe("cascade snapshot integrity", () => {
  it("accepts complete ordered target indices", () => {
    expect(() =>
      assertIndexedEntries(
        [{ targetIndex: 0 }, { targetIndex: 1 }],
        2,
        "candidate",
      ),
    ).not.toThrow();
  });

  it("rejects dropped, repeated, and reordered targets", () => {
    expect(() =>
      assertIndexedEntries([{ targetIndex: 0 }], 2, "candidate"),
    ).toThrow("target count 1 != 2");
    expect(() =>
      assertIndexedEntries(
        [{ targetIndex: 0 }, { targetIndex: 0 }],
        2,
        "candidate",
      ),
    ).toThrow("repeats target 0");
    expect(() =>
      assertIndexedEntries(
        [{ targetIndex: 1 }, { targetIndex: 0 }],
        2,
        "candidate",
      ),
    ).toThrow("entry 0 reports target 1");
  });

  it("keeps enumerated longhands and declared custom properties", () => {
    expect(
      mergeComputedPropertyNames(
        ["margin-top", "padding-left"],
        ["margin-top", "--fixture-token"],
      ),
    ).toEqual(["--fixture-token", "margin-top", "padding-left"]);
  });
});

describe("repository baseline provenance", () => {
  it("matches the checked-in manifest digest and shape", () => {
    const directory = path.dirname(fileURLToPath(import.meta.url));
    const baseline = fs.readFileSync(
      path.join(directory, "baselines/App.pre-owner.css"),
    );
    const manifest = JSON.parse(
      fs.readFileSync(
        path.join(directory, "baselines/App.pre-owner.json"),
        "utf8",
      ),
    );

    expect(crypto.createHash("sha256").update(baseline).digest("hex")).toBe(
      manifest.source_sha256,
    );
    expect(baseline.byteLength).toBe(manifest.source_bytes);
    expect(baseline.toString("utf8").match(/\n/g)).toHaveLength(
      manifest.source_lines,
    );
  });

  it("matches the frozen owner selector corpus manifest", () => {
    const directory = path.dirname(fileURLToPath(import.meta.url));
    const manifest = JSON.parse(
      fs.readFileSync(
        path.join(directory, "baselines/owner-selector-corpus.json"),
        "utf8",
      ),
    );
    const css = manifest.owner_files
      .map((file) => fs.readFileSync(file, "utf8"))
      .join("\n");

    expect(manifest.owner_files).toHaveLength(manifest.owner_file_count);
    expect(crypto.createHash("sha256").update(css).digest("hex")).toBe(
      manifest.combined_sha256,
    );
    expect(Buffer.byteLength(css)).toBe(manifest.combined_bytes);
    expect(css.match(/\n/g)).toHaveLength(manifest.combined_lines);
  });
});
