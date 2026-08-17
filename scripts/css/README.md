# CSS cascade parity gate

`npm run css:check-cascade` follows static and literal lazy imports from
`src/main.tsx`, requires that graph to reach every stylesheet in the frozen
owner corpus, and compares those reachable owners with the pre-owner-file
stylesheet in `baselines/App.pre-owner.css`. The candidate is assembled in the
reviewed corpus order because independently loaded lazy chunks do not have one
global runtime insertion order. The baseline manifest records the artifact's
SHA-256, byte count, line count, source checkpoint, and final newline; the gate
verifies it before use.

Selector discovery comes from the separately checksummed owner list in
`baselines/owner-selector-corpus.json`, not from the candidate assembly. This is
intentional: if a later TypeScript or route-loading cutover omits an owner, its
selector fixtures remain present and expose the missing CSS. Owner CSS changes
require an explicit corpus-manifest update while this migration gate is active.

The gate builds two complementary fixture sets:

- TSX-derived targets preserve literal same-element class combinations and
  same-file ancestor classes. Each target is exercised in its base state and a
  combined forced-state variant; individual state ownership is covered by the
  exact selector fixtures.
- CSS-derived targets synthesize every supported selector branch, including
  cross-component-looking ancestor chains, combinators, attributes, ancestor
  states, and simple relational selectors. Every generated target must satisfy
  its transformed selector through `Element.matches()` or the gate fails.

This is selector-structure coverage, not a proof of the rendered React tree.
For example, `.game-page .material-icons` is synthesized and a mutation to it
is caught even though the gate cannot establish that those classes really nest
across component boundaries. Source tracing or a focused E2E test remains the
authority for actual cross-component composition.

Chromium supplies the enumerated computed-property set, so shorthand
declarations are checked through their computed longhands. Declared custom
properties are added explicitly. Every snapshot must contain the exact ordered
target index set before hashes are compared.

The full viewport/reduced-motion matrix runs in `.githooks/pre-push`. `--quick`
is a four-configuration diagnostic mode; `--baseline` and `--candidate` are
available for explicit mutation checks. A CSS `--candidate` keeps the old
single-stylesheet diagnostic behavior; a TypeScript/JavaScript candidate is
treated as a runtime entry module and includes static plus literal lazy imports.

The module traversal intentionally follows only relative or Vite root-relative
literal imports. Bare specifiers (including Vite aliases), non-literal
`import()` expressions, and `import.meta.glob` calls are excluded; none
currently carries application CSS. `g-css-route-audit` tracks the stricter
per-route reachability check and explicit coverage of those exclusions.
