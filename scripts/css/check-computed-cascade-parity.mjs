#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import postcss from "postcss";
import ts from "typescript";
import {
  TARGET_STATE_MARKERS,
  assertIndexedEntries,
  buildFixtureDescriptors,
  enrichCssScenariosFromTsx,
  extractCssSelectorScenarios,
  forcePseudoStates,
  mergeComputedPropertyNames,
} from "./cascade-parity-lib.mjs";

const WIDTHS = [320, 620, 659, 720, 721, 768, 900, 1099];
const HEIGHTS = [640, 680, 760, 810];
const DEFAULT_WIDTH = 1440;
const DEFAULT_HEIGHT = 900;
const MAX_VARIANTS_PER_ATTRIBUTE = 64;
const MAX_RECORDED_DIFFERENCES = 500;
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_BASELINE = path.join(
  SCRIPT_DIRECTORY,
  "baselines/App.pre-owner.css",
);
const DEFAULT_BASELINE_MANIFEST = path.join(
  SCRIPT_DIRECTORY,
  "baselines/App.pre-owner.json",
);
const DEFAULT_SELECTOR_FIXTURE_MANIFEST = path.join(
  SCRIPT_DIRECTORY,
  "baselines/owner-selector-corpus.json",
);

const usage = () => {
  console.error(
    "Usage: node scripts/css/check-computed-cascade-parity.mjs " +
      "[--baseline <css>] [--candidate src/App.css] [--output <json>] [--quick]",
  );
};

const parseArgs = (argv) => {
  const result = {
    baseline: DEFAULT_BASELINE,
    candidate: "src/App.css",
    output: undefined,
    quick: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--baseline") result.baseline = argv[++index];
    else if (argument === "--candidate") result.candidate = argv[++index];
    else if (argument === "--output") result.output = argv[++index];
    else if (argument === "--quick") result.quick = true;
    else throw new Error(`Unknown argument: ${argument}`);
  }
  return result;
};

const walkFiles = (directory) =>
  fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return walkFiles(fullPath);
    return entry.isFile() ? [fullPath] : [];
  });

const splitClasses = (value) =>
  value
    .split(/\s+/)
    .map((token) => token.trim())
    .filter((token) => /^-?[_a-zA-Z]+[_a-zA-Z0-9-]*$/.test(token));

const uniqueStrings = (values) => [...new Set(values)];

const combineVariants = (left, right) => {
  const result = [];
  for (const leftValue of left) {
    for (const rightValue of right) {
      result.push(`${leftValue}${rightValue}`);
      if (result.length >= MAX_VARIANTS_PER_ATTRIBUTE) return result;
    }
  }
  return result;
};

const expressionVariants = (expression) => {
  if (!expression) return [""];
  if (
    ts.isStringLiteral(expression) ||
    ts.isNoSubstitutionTemplateLiteral(expression)
  ) {
    return [expression.text];
  }
  if (
    ts.isParenthesizedExpression(expression) ||
    ts.isAsExpression(expression) ||
    ts.isTypeAssertionExpression(expression)
  ) {
    return expressionVariants(expression.expression);
  }
  if (ts.isConditionalExpression(expression)) {
    const whenTrue = expressionVariants(expression.whenTrue);
    const whenFalse = expressionVariants(expression.whenFalse);
    if (!whenTrue || !whenFalse) return undefined;
    return uniqueStrings([...whenTrue, ...whenFalse]).slice(
      0,
      MAX_VARIANTS_PER_ATTRIBUTE,
    );
  }
  if (ts.isBinaryExpression(expression)) {
    if (expression.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      const left = expressionVariants(expression.left);
      const right = expressionVariants(expression.right);
      return left && right ? combineVariants(left, right) : undefined;
    }
    if (expression.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken) {
      const right = expressionVariants(expression.right);
      return right ? uniqueStrings(["", ...right]) : undefined;
    }
    if (expression.operatorToken.kind === ts.SyntaxKind.BarBarToken) {
      const left = expressionVariants(expression.left);
      const right = expressionVariants(expression.right);
      return left && right
        ? uniqueStrings([...left, ...right]).slice(0, MAX_VARIANTS_PER_ATTRIBUTE)
        : undefined;
    }
  }
  if (ts.isTemplateExpression(expression)) {
    let variants = [expression.head.text];
    for (const span of expression.templateSpans) {
      const expressionValues = expressionVariants(span.expression);
      if (!expressionValues) return undefined;
      variants = combineVariants(variants, expressionValues).map(
        (value) => `${value}${span.literal.text}`,
      );
    }
    return variants;
  }
  return undefined;
};

const literalFragments = (node) => {
  const fragments = [];
  const visit = (child) => {
    if (
      ts.isStringLiteral(child) ||
      ts.isNoSubstitutionTemplateLiteral(child) ||
      ts.isTemplateHead(child) ||
      ts.isTemplateMiddle(child) ||
      ts.isTemplateTail(child)
    ) {
      fragments.push(child.text);
    }
    ts.forEachChild(child, visit);
  };
  visit(node);
  return fragments;
};

const jsxTagName = (attribute) => {
  const opening = attribute.parent?.parent;
  if (!opening || (!ts.isJsxOpeningElement(opening) && !ts.isJsxSelfClosingElement(opening))) {
    return "div";
  }
  const tagName = opening.tagName.getText();
  return /^[a-z][a-z0-9-]*$/.test(tagName) ? tagName : "div";
};

const attributeClassValues = (attribute) => {
  const initializer = attribute.initializer;
  if (!initializer) return { values: [], resolved: false };
  if (ts.isStringLiteral(initializer)) {
    return { values: [initializer.text], resolved: true };
  }
  if (!ts.isJsxExpression(initializer) || !initializer.expression) {
    return { values: [], resolved: false };
  }
  const variants = expressionVariants(initializer.expression);
  if (variants) return { values: variants, resolved: true };
  const fragments = literalFragments(initializer.expression);
  return {
    values: fragments.length > 0 ? [fragments.join(" ")] : [],
    resolved: false,
  };
};

const findClassAttribute = (opening) =>
  opening.attributes.properties.find(
    (property) =>
      ts.isJsxAttribute(property) && property.name.getText() === "className",
  );

const ancestorClassLayers = (attribute) => {
  const result = [];
  const opening = attribute.parent?.parent;
  const element = opening?.parent;
  const firstAncestor =
    element && ts.isJsxElement(element) && element.openingElement === opening
      ? element.parent
      : element;
  for (let current = firstAncestor; current; current = current.parent) {
    if (!ts.isJsxElement(current)) continue;
    const classAttribute = findClassAttribute(current.openingElement);
    if (!classAttribute) continue;
    const extracted = attributeClassValues(classAttribute);
    const layer = new Set();
    for (const value of extracted.values) {
      for (const className of splitClasses(value)) layer.add(className);
    }
    if (layer.size > 0) result.push([...layer]);
  }
  return result.reverse();
};

const expandUnresolvedClassNames = (classNames, knownCssClasses) => {
  const result = [classNames];
  for (const className of classNames) {
    const danglingPrefix = className.endsWith("-");
    const modifierPrefix = danglingPrefix ? className : `${className}--`;
    const matches = knownCssClasses.filter((knownClass) =>
      knownClass.startsWith(modifierPrefix),
    );
    for (const match of matches) {
      const expanded = danglingPrefix
        ? classNames.map((candidate) =>
            candidate === className ? match : candidate,
          )
        : [...classNames, match];
      result.push(expanded);
      if (result.length >= MAX_VARIANTS_PER_ATTRIBUTE) return result;
    }
  }
  return result;
};

const extractTsxScenarios = (knownCssClasses) => {
  const scenarios = [];
  let attributeCount = 0;
  let unresolvedAttributeCount = 0;
  let expandedUnresolvedScenarioCount = 0;
  const unresolvedSources = [];
  const files = walkFiles("src").filter((file) => file.endsWith(".tsx"));
  for (const file of files) {
    const sourceText = fs.readFileSync(file, "utf8");
    const sourceFile = ts.createSourceFile(
      file,
      sourceText,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TSX,
    );
    const visit = (node) => {
      if (ts.isJsxAttribute(node) && node.name.getText() === "className") {
        attributeCount += 1;
        const extracted = attributeClassValues(node);
        const source = `${file}:${sourceFile.getLineAndCharacterOfPosition(node.getStart()).line + 1}`;
        if (!extracted.resolved) {
          unresolvedAttributeCount += 1;
          unresolvedSources.push(source);
        }
        for (const value of extracted.values) {
          const classNames = splitClasses(value);
          if (classNames.length === 0) continue;
          const classNameVariants = extracted.resolved
            ? [classNames]
            : expandUnresolvedClassNames(classNames, knownCssClasses);
          expandedUnresolvedScenarioCount += classNameVariants.length - 1;
          for (const classNameVariant of classNameVariants) {
            const ancestors = ancestorClassLayers(node).map((classNames) => ({
              tag: "div",
              classNames,
              attributes: {},
              requiredChildren: [],
            }));
            scenarios.push({
              nodes: [
                ...ancestors,
                {
                  tag: jsxTagName(node),
                  classNames: classNameVariant,
                  attributes: {},
                  requiredChildren: [],
                },
              ],
              combinators: ancestors.map(() => " "),
              pseudoElements: [],
              source,
              kind: "tsx",
            });
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }
  return {
    scenarios,
    attributeCount,
    unresolvedAttributeCount,
    expandedUnresolvedScenarioCount,
    unresolvedSources,
  };
};

const deduplicateScenarios = (scenarios) => {
  const result = [];
  const seen = new Set();
  for (const scenario of scenarios) {
    const normalized = {
      ...scenario,
      nodes: scenario.nodes.map((node) => ({
        ...node,
        classNames: uniqueStrings(node.classNames).sort(),
      })),
    };
    const key = JSON.stringify([normalized.nodes, normalized.combinators]);
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(normalized);
  }
  return result;
};

const expandImports = (entryPath, stack = []) => {
  const absolutePath = path.resolve(entryPath);
  if (stack.includes(absolutePath)) {
    throw new Error(`CSS import cycle: ${[...stack, absolutePath].join(" -> ")}`);
  }
  const source = fs.readFileSync(absolutePath, "utf8");
  return source.replace(
    /@import\s+["']([^"']+)["']\s*;/g,
    (_match, importPath) =>
      expandImports(path.resolve(path.dirname(absolutePath), importPath), [
        ...stack,
        absolutePath,
      ]),
  );
};

const configurations = (quick = false) => {
  if (quick) {
    return [
      { width: 320, height: DEFAULT_HEIGHT, reducedMotion: false },
      { width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT, reducedMotion: false },
      { width: 320, height: DEFAULT_HEIGHT, reducedMotion: true },
      { width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT, reducedMotion: true },
    ];
  }
  const result = [];
  for (const reducedMotion of [false, true]) {
    for (const width of WIDTHS) {
      result.push({ width, height: DEFAULT_HEIGHT, reducedMotion });
    }
    for (const height of HEIGHTS) {
      result.push({ width: DEFAULT_WIDTH, height, reducedMotion });
    }
  }
  return result;
};

const buildFixturesFunction = ({ fixtures }) => {
  const createNode = (descriptor) => {
    const element = document.createElement(descriptor.tag);
    if (descriptor.id) element.id = descriptor.id;
    element.className = descriptor.classNames.join(" ");
    for (const [name, value] of Object.entries(descriptor.attributes)) {
      element.setAttribute(name, value);
    }
    for (const requiredChild of descriptor.requiredChildren) {
      element.appendChild(createNode(requiredChild));
    }
    return element;
  };

  const targetEntries = [];
  const selectorValidations = [];
  for (const fixture of fixtures) {
    const section = document.createElement("section");
    section.dataset.cascadeScenario = String(fixture.scenarioIndex);
    let current;
    fixture.nodes.forEach((descriptor, index) => {
      const element = createNode(descriptor);
      if (index === 0) {
        section.appendChild(element);
      } else {
        const combinator = fixture.combinators[index - 1];
        if (combinator === " " || combinator === ">") {
          current.appendChild(element);
        } else {
          current.parentNode.appendChild(element);
        }
      }
      current = element;
    });
    current.dataset.cascadeTarget = String(fixture.targetIndex);
    document.body.appendChild(section);
    targetEntries.push({ targetIndex: fixture.targetIndex });
    if (fixture.matchSelector) {
      try {
        selectorValidations.push({
          targetIndex: fixture.targetIndex,
          selector: fixture.matchSelector,
          matched: current.matches(fixture.matchSelector),
        });
      } catch (error) {
        selectorValidations.push({
          targetIndex: fixture.targetIndex,
          selector: fixture.matchSelector,
          matched: false,
          error: String(error),
        });
      }
    }
  }
  return { targetEntries, selectorValidations };
};

const assertFixtureBuild = (result, targets, selectorScenarioCount, label) => {
  assertIndexedEntries(result.targetEntries, targets.length, `${label} fixture`);
  if (result.selectorValidations.length !== selectorScenarioCount) {
    throw new Error(
      `${label} validated ${result.selectorValidations.length} selectors, expected ${selectorScenarioCount}`,
    );
  }
  const failures = result.selectorValidations.filter((entry) => !entry.matched);
  if (failures.length > 0) {
    throw new Error(
      `${label} generated selector fixtures did not match:\n${JSON.stringify(failures.slice(0, 20), null, 2)}`,
    );
  }
};

const enumeratedComputedPropertiesFunction = () =>
  Array.from(getComputedStyle(document.documentElement));

const styleHashFunction = ({ properties, targetPseudos }) => {
  const hashString = (hash, value) => {
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  };
  const hashStyle = (element, pseudo) => {
    const style = getComputedStyle(element, pseudo);
    let first = 2166136261;
    let second = 2246822507;
    for (const property of properties) {
      const record = `${property}\u0000${style.getPropertyValue(property)}\u0000`;
      first = hashString(first, record);
      second = hashString(second, `${record.length}:${record}`);
    }
    return [first, second];
  };
  document.getAnimations().forEach((animation) => animation.cancel());
  return [...document.querySelectorAll("[data-cascade-target]")].map(
    (element) => {
      const targetIndex = Number(element.dataset.cascadeTarget);
      return {
        targetIndex,
        hashes: targetPseudos[targetIndex].map((pseudo) => [
          pseudo,
          ...hashStyle(element, pseudo || null),
        ]),
      };
    },
  );
};

const styleRecordFunction = ({ targetIndex, pseudo, properties }) => {
  const element = document.querySelector(
    `[data-cascade-target="${targetIndex}"]`,
  );
  if (!element) throw new Error(`Missing cascade target ${targetIndex}`);
  const style = getComputedStyle(element, pseudo || null);
  return Object.fromEntries(
    properties.map((property) => [property, style.getPropertyValue(property)]),
  );
};

const declaredCustomProperties = (...stylesheets) => {
  const properties = new Set();
  for (const css of stylesheets) {
    postcss.parse(css).walkDecls((declaration) => {
      if (declaration.prop.startsWith("--")) properties.add(declaration.prop);
    });
  }
  return [...properties].sort();
};

const sha256 = (contents) =>
  crypto.createHash("sha256").update(contents).digest("hex");

const verifyRepositoryBaseline = (baselinePath, contents) => {
  if (path.resolve(baselinePath) !== path.resolve(DEFAULT_BASELINE)) return null;
  const manifest = JSON.parse(
    fs.readFileSync(DEFAULT_BASELINE_MANIFEST, "utf8"),
  );
  const actual = {
    artifact: path.basename(baselinePath),
    source_sha256: sha256(contents),
    source_bytes: Buffer.byteLength(contents),
    source_lines: (contents.match(/\n/g) ?? []).length,
    final_newline: contents.endsWith("\n"),
  };
  for (const key of [
    "artifact",
    "source_sha256",
    "source_bytes",
    "source_lines",
    "final_newline",
  ]) {
    if (actual[key] !== manifest[key]) {
      throw new Error(
        `Repository CSS baseline ${key} mismatch: ${JSON.stringify(actual[key])} != ${JSON.stringify(manifest[key])}`,
      );
    }
  }
  return manifest;
};

const readRepositorySelectorCorpus = () => {
  const manifest = JSON.parse(
    fs.readFileSync(DEFAULT_SELECTOR_FIXTURE_MANIFEST, "utf8"),
  );
  if (!Array.isArray(manifest.owner_files) || manifest.owner_files.length === 0) {
    throw new Error("Repository selector corpus manifest has no owner files");
  }
  const css = manifest.owner_files
    .map((file) => fs.readFileSync(path.resolve(file), "utf8"))
    .join("\n");
  const actual = {
    owner_file_count: manifest.owner_files.length,
    combined_sha256: sha256(css),
    combined_bytes: Buffer.byteLength(css),
    combined_lines: (css.match(/\n/g) ?? []).length,
    final_newline: css.endsWith("\n"),
  };
  for (const key of [
    "owner_file_count",
    "combined_sha256",
    "combined_bytes",
    "combined_lines",
    "final_newline",
  ]) {
    if (actual[key] !== manifest[key]) {
      throw new Error(
        `Repository selector corpus ${key} mismatch: ${JSON.stringify(actual[key])} != ${JSON.stringify(manifest[key])}`,
      );
    }
  }
  return { css, manifest };
};

const diffRecords = (baseline, candidate) => {
  const properties = new Set([
    ...Object.keys(baseline),
    ...Object.keys(candidate),
  ]);
  return [...properties]
    .filter((property) => baseline[property] !== candidate[property])
    .map((property) => ({
      property,
      baseline: baseline[property],
      candidate: candidate[property],
    }));
};

const main = async () => {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    usage();
    throw error;
  }
  const baselineAppCss = fs.readFileSync(args.baseline, "utf8");
  const baselineManifest = verifyRepositoryBaseline(
    args.baseline,
    baselineAppCss,
  );
  const selectorCorpus = readRepositorySelectorCorpus();
  const candidateAppCss = expandImports(args.candidate);
  const indexCss = fs.readFileSync("src/index.css", "utf8");
  const baselineCss = forcePseudoStates(`${indexCss}\n${baselineAppCss}`);
  const candidateCss = forcePseudoStates(`${indexCss}\n${candidateAppCss}`);
  const knownCssClasses = uniqueStrings(
    [...selectorCorpus.css.matchAll(/\.(-?[_a-zA-Z]+[_a-zA-Z0-9-]*)/g)].map(
      (match) => match[1],
    ),
  );
  const extracted = extractTsxScenarios(knownCssClasses);
  const tsxScenarios = deduplicateScenarios(extracted.scenarios);
  // Fixture discovery is deliberately independent from candidate assembly.
  // Otherwise an omitted owner would erase both its CSS and the structure that
  // should expose the omission during the import/lazy-loading cutovers.
  const selectorExtraction = extractCssSelectorScenarios(selectorCorpus.css);
  if (selectorExtraction.unsupported.length > 0) {
    throw new Error(
      `Unsupported CSS selector fixtures:\n${JSON.stringify(selectorExtraction.unsupported.slice(0, 40), null, 2)}`,
    );
  }
  const cssSelectorScenarios = enrichCssScenariosFromTsx(
    selectorExtraction.scenarios,
    tsxScenarios,
  );
  const scenarios = [...tsxScenarios, ...cssSelectorScenarios];
  const { fixtures, targets } = buildFixtureDescriptors(scenarios);
  const matrixConfigurations = configurations(args.quick);
  const scenarioIsolationCss = `[data-cascade-scenario] {
    contain: strict !important;
    display: block !important;
    position: relative !important;
    width: 100vw !important;
    height: 100vh !important;
    overflow: hidden !important;
  }`;
  const html = (css) =>
    `<!doctype html><html><head><style>${css}\n${scenarioIsolationCss}</style></head><body></body></html>`;

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const baselinePage = await context.newPage();
  const candidatePage = await context.newPage();
  await baselinePage.setContent(html(baselineCss), { waitUntil: "load" });
  await candidatePage.setContent(html(candidateCss), { waitUntil: "load" });

  const [baselineFixtureBuild, candidateFixtureBuild] = await Promise.all([
    baselinePage.evaluate(buildFixturesFunction, { fixtures }),
    candidatePage.evaluate(buildFixturesFunction, { fixtures }),
  ]);
  assertFixtureBuild(
    baselineFixtureBuild,
    targets,
    cssSelectorScenarios.length,
    "baseline",
  );
  assertFixtureBuild(
    candidateFixtureBuild,
    targets,
    cssSelectorScenarios.length,
    "candidate",
  );
  const [baselineComputedProperties, candidateComputedProperties] =
    await Promise.all([
      baselinePage.evaluate(enumeratedComputedPropertiesFunction),
      candidatePage.evaluate(enumeratedComputedPropertiesFunction),
    ]);
  const properties = mergeComputedPropertyNames(
    baselineComputedProperties,
    candidateComputedProperties,
    declaredCustomProperties(baselineCss, candidateCss),
  );
  const targetPseudos = targets.map((target) => target.pseudos);
  console.log(
    `cascade matrix: ${scenarios.length} scenarios ` +
      `(${tsxScenarios.length} TSX + ${cssSelectorScenarios.length} selector), ` +
      `${targets.length} targets, ${properties.length} computed properties, ` +
      `${matrixConfigurations.length} configurations`,
  );

  const differences = [];
  const recordedDifferenceKeys = new Set();
  const configurationResults = [];
  try {
    for (const [configurationIndex, configuration] of matrixConfigurations.entries()) {
      await Promise.all([
        baselinePage.setViewportSize({
          width: configuration.width,
          height: configuration.height,
        }),
        candidatePage.setViewportSize({
          width: configuration.width,
          height: configuration.height,
        }),
        baselinePage.emulateMedia({
          reducedMotion: configuration.reducedMotion ? "reduce" : "no-preference",
        }),
        candidatePage.emulateMedia({
          reducedMotion: configuration.reducedMotion ? "reduce" : "no-preference",
        }),
      ]);
      const [baselineHashes, candidateHashes] = await Promise.all([
        baselinePage.evaluate(styleHashFunction, { properties, targetPseudos }),
        candidatePage.evaluate(styleHashFunction, { properties, targetPseudos }),
      ]);
      assertIndexedEntries(
        baselineHashes,
        targets.length,
        `baseline ${configuration.width}x${configuration.height}`,
      );
      assertIndexedEntries(
        candidateHashes,
        targets.length,
        `candidate ${configuration.width}x${configuration.height}`,
      );
      const mismatchedTargets = [];
      for (let targetIndex = 0; targetIndex < targets.length; targetIndex += 1) {
        if (
          JSON.stringify(baselineHashes[targetIndex].hashes) !==
          JSON.stringify(candidateHashes[targetIndex].hashes)
        ) {
          mismatchedTargets.push(targetIndex);
        }
      }
      configurationResults.push({
        ...configuration,
        target_count: targets.length,
        mismatched_target_count: mismatchedTargets.length,
      });

      for (const targetIndex of mismatchedTargets) {
        if (differences.length >= MAX_RECORDED_DIFFERENCES) break;
        for (const pseudo of targets[targetIndex].pseudos) {
          const [baselineRecord, candidateRecord] = await Promise.all([
            baselinePage.evaluate(styleRecordFunction, {
              targetIndex,
              pseudo,
              properties,
            }),
            candidatePage.evaluate(styleRecordFunction, {
              targetIndex,
              pseudo,
              properties,
            }),
          ]);
          const propertyDifferences = diffRecords(
            baselineRecord,
            candidateRecord,
          );
          if (propertyDifferences.length === 0) continue;
          const differenceKey = JSON.stringify([
            configuration,
            targets[targetIndex].scenarioIndex,
            pseudo,
            propertyDifferences,
          ]);
          if (recordedDifferenceKeys.has(differenceKey)) continue;
          recordedDifferenceKeys.add(differenceKey);
          differences.push({
            configuration,
            target_index: targetIndex,
            target: targets[targetIndex],
            pseudo,
            properties: propertyDifferences,
          });
          if (differences.length >= MAX_RECORDED_DIFFERENCES) break;
        }
      }
      console.log(
        `cascade matrix ${configurationIndex + 1}/${matrixConfigurations.length}: ` +
          `${configuration.width}x${configuration.height}, ` +
          `${configuration.reducedMotion ? "reduce" : "no-preference"}, ` +
          `${mismatchedTargets.length} mismatches`,
      );
    }
  } finally {
    await browser.close();
  }

  const mismatchedConfigurationCount = configurationResults.filter(
    (configuration) => configuration.mismatched_target_count > 0,
  ).length;
  const structuredSelectorScenarioCount = cssSelectorScenarios.filter(
    (scenario) => scenario.nodes.length > 1,
  ).length;
  const ancestorStateSelectorScenarioCount =
    cssSelectorScenarios.filter((scenario) =>
      scenario.nodes
        .slice(0, -1)
        .some((node) =>
          node.classNames.some((className) =>
            className.startsWith("__cascade-force-"),
          ),
        ),
    ).length;
  const attributeSelectorScenarioCount = cssSelectorScenarios.filter(
    (scenario) =>
      scenario.nodes.some(
        (node) => Object.keys(node.attributes).length > 0,
      ),
  ).length;
  const relationalPseudoSelectorScenarioCount =
    cssSelectorScenarios.filter((scenario) =>
      scenario.nodes.some((node) => node.requiredChildren.length > 0),
    ).length;
  const result = {
    coverage_model: {
      css_selector_structures:
        "Synthesized from every supported selector branch and validated with Element.matches().",
      tsx_same_element_invariants:
        "Literal class invariants are intersected from matching TSX targets; same-file ancestry narrows the match when available.",
      runtime_component_composition:
        "Not proven by this gate. Cross-component ancestry is a selector-shaped fixture and still requires source tracing or focused E2E coverage.",
    },
    baseline: path.resolve(args.baseline),
    baseline_sha256: sha256(baselineAppCss),
    repository_baseline_manifest: baselineManifest,
    selector_fixture_manifest: selectorCorpus.manifest,
    candidate: path.resolve(args.candidate),
    widths: WIDTHS,
    heights: HEIGHTS,
    reduced_motion_modes: ["no-preference", "reduce"],
    configuration_count: configurationResults.length,
    tsx_class_name_attribute_count: extracted.attributeCount,
    tsx_unresolved_attribute_count: extracted.unresolvedAttributeCount,
    css_expanded_unresolved_scenario_count:
      extracted.expandedUnresolvedScenarioCount,
    unresolved_class_name_sources: extracted.unresolvedSources,
    tsx_scenario_count: tsxScenarios.length,
    css_selector_branch_occurrence_count:
      selectorExtraction.branchOccurrenceCount,
    css_selector_unique_branch_count: selectorExtraction.uniqueBranchCount,
    css_selector_scenario_count: cssSelectorScenarios.length,
    css_selector_validated_count: cssSelectorScenarios.length,
    css_selector_unsupported_count: selectorExtraction.unsupported.length,
    css_selector_tsx_enriched_count: cssSelectorScenarios.filter(
      (scenario) => scenario.tsxEnrichmentMatchCount > 0,
    ).length,
    css_selector_target_only_enriched_count: cssSelectorScenarios.filter(
      (scenario) => scenario.tsxEnrichmentMode === "target-only",
    ).length,
    structured_selector_scenario_count: structuredSelectorScenarioCount,
    ancestor_state_selector_scenario_count:
      ancestorStateSelectorScenarioCount,
    attribute_selector_scenario_count: attributeSelectorScenarioCount,
    relational_pseudo_selector_scenario_count:
      relationalPseudoSelectorScenarioCount,
    scenario_count: scenarios.length,
    target_count_per_configuration: targets.length,
    compared_property_count: properties.length,
    compared_properties: properties,
    mismatched_configuration_count: mismatchedConfigurationCount,
    total_mismatched_targets: configurationResults.reduce(
      (sum, configuration) => sum + configuration.mismatched_target_count,
      0,
    ),
    recorded_difference_limit: MAX_RECORDED_DIFFERENCES,
    differences,
    configurations: configurationResults,
  };
  const serialized = `${JSON.stringify(result, null, 2)}\n`;
  if (args.output) fs.writeFileSync(args.output, serialized);
  console.log(
    JSON.stringify(
      {
        configuration_count: result.configuration_count,
        scenario_count: result.scenario_count,
        target_count_per_configuration: result.target_count_per_configuration,
        tsx_class_name_attribute_count: result.tsx_class_name_attribute_count,
        tsx_unresolved_attribute_count: result.tsx_unresolved_attribute_count,
        css_expanded_unresolved_scenario_count:
          result.css_expanded_unresolved_scenario_count,
        css_selector_scenario_count: result.css_selector_scenario_count,
        css_selector_validated_count: result.css_selector_validated_count,
        css_selector_unsupported_count: result.css_selector_unsupported_count,
        css_selector_tsx_enriched_count:
          result.css_selector_tsx_enriched_count,
        css_selector_target_only_enriched_count:
          result.css_selector_target_only_enriched_count,
        structured_selector_scenario_count:
          result.structured_selector_scenario_count,
        ancestor_state_selector_scenario_count:
          result.ancestor_state_selector_scenario_count,
        attribute_selector_scenario_count:
          result.attribute_selector_scenario_count,
        relational_pseudo_selector_scenario_count:
          result.relational_pseudo_selector_scenario_count,
        mismatched_configuration_count: result.mismatched_configuration_count,
        total_mismatched_targets: result.total_mismatched_targets,
        recorded_difference_count: result.differences.length,
      },
      null,
      2,
    ),
  );
  if (mismatchedConfigurationCount > 0) process.exitCode = 1;
};

await main();
