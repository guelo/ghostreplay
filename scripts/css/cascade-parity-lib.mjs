import crypto from "node:crypto";
import * as csstree from "css-tree";
import fs from "node:fs";
import path from "node:path";
import postcss from "postcss";
import ts from "typescript";

const SCRIPT_EXTENSIONS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"];
const MODULE_EXTENSIONS = [...SCRIPT_EXTENSIONS, ".css"];

// This deliberately models relative and Vite root-relative imports only. Bare
// specifiers (including Vite aliases), non-literal import() expressions, and
// import.meta.glob are outside the traversal. They do not currently carry
// application CSS; g-css-route-audit tracks a stricter per-route audit and
// explicit handling for these exclusions.
const runtimeImportSpecifiers = (source, filePath) => {
  const sourceFile = ts.createSourceFile(
    filePath,
    source,
    ts.ScriptTarget.Latest,
    true,
    filePath.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const specifiers = [];
  const addLiteral = (node) => {
    if (node && ts.isStringLiteralLike(node)) specifiers.push(node.text);
  };
  const hasRuntimeImport = (clause) => {
    if (!clause) return true;
    if (clause.isTypeOnly) return false;
    if (clause.name) return true;
    if (!clause.namedBindings) return false;
    if (ts.isNamespaceImport(clause.namedBindings)) return true;
    return (
      ts.isNamedImports(clause.namedBindings) &&
      clause.namedBindings.elements.some((element) => !element.isTypeOnly)
    );
  };
  const hasRuntimeExport = (node) => {
    if (node.isTypeOnly) return false;
    if (!node.exportClause || ts.isNamespaceExport(node.exportClause)) return true;
    return node.exportClause.elements.some((element) => !element.isTypeOnly);
  };
  const visit = (node) => {
    if (ts.isImportDeclaration(node)) {
      if (hasRuntimeImport(node.importClause)) addLiteral(node.moduleSpecifier);
      return;
    }
    if (ts.isExportDeclaration(node)) {
      if (hasRuntimeExport(node)) addLiteral(node.moduleSpecifier);
      return;
    }
    if (
      ts.isCallExpression(node) &&
      node.expression.kind === ts.SyntaxKind.ImportKeyword
    ) {
      addLiteral(node.arguments[0]);
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return specifiers;
};

const resolveLocalModule = (fromPath, rawSpecifier, rootDir) => {
  if (!rawSpecifier.startsWith(".") && !path.isAbsolute(rawSpecifier)) {
    return null;
  }
  const specifier = rawSpecifier.replace(/[?#].*$/, "");
  const unresolved = specifier.startsWith("/")
    ? path.resolve(rootDir, specifier.slice(1))
    : path.resolve(path.dirname(fromPath), specifier);
  const candidates = [
    unresolved,
    ...MODULE_EXTENSIONS.map((extension) => `${unresolved}${extension}`),
    ...MODULE_EXTENSIONS.map((extension) =>
      path.join(unresolved, `index${extension}`),
    ),
  ];
  const resolved = candidates.find((candidate) => {
    try {
      return fs.statSync(candidate).isFile();
    } catch {
      return false;
    }
  });
  if (!resolved) {
    throw new Error(
      `Cannot resolve runtime import ${JSON.stringify(rawSpecifier)} from ${fromPath}`,
    );
  }
  return resolved;
};

export const expandCssImports = (entryPath, stack = []) => {
  const absolutePath = path.resolve(entryPath);
  if (stack.includes(absolutePath)) {
    throw new Error(`CSS import cycle: ${[...stack, absolutePath].join(" -> ")}`);
  }
  const source = fs.readFileSync(absolutePath, "utf8");
  return source.replace(
    /@import\s+["']([^"']+)["']\s*;/g,
    (_match, importPath) =>
      expandCssImports(path.resolve(path.dirname(absolutePath), importPath), [
        ...stack,
        absolutePath,
      ]),
  );
};

export const assertPostOwnerAdditionsAreAdditive = (
  baselineCss,
  additionsCss,
) => {
  const isKeyframeStep = (rule) =>
    rule.parent?.type === "atrule" &&
    /keyframes$/i.test(rule.parent.name ?? "");
  const baselineSelectors = new Set();
  postcss.parse(baselineCss).walkRules((rule) => {
    if (isKeyframeStep(rule)) return;
    for (const selector of rule.selectors) {
      baselineSelectors.add(selector.trim());
    }
  });
  postcss.parse(additionsCss).walkRules((rule) => {
    if (isKeyframeStep(rule)) return;
    for (const selector of rule.selectors) {
      const normalizedSelector = selector.trim();
      if (baselineSelectors.has(normalizedSelector)) {
        throw new Error(
          `Post-owner overlay may only add selectors; ${JSON.stringify(normalizedSelector)} already exists in the frozen baseline`,
        );
      }
    }
  });
};

export const assembleCssFromModuleGraph = (
  entryPath,
  { rootDir = process.cwd() } = {},
) => {
  const absoluteEntry = path.resolve(entryPath);
  const visited = new Set();
  const modules = [];
  const stylesheets = [];
  const visit = (modulePath) => {
    const absolutePath = path.resolve(modulePath);
    if (visited.has(absolutePath)) return;
    visited.add(absolutePath);
    modules.push(absolutePath);
    const extension = path.extname(absolutePath);
    if (extension === ".css") {
      stylesheets.push(absolutePath);
      return;
    }
    if (!SCRIPT_EXTENSIONS.includes(extension)) return;
    const source = fs.readFileSync(absolutePath, "utf8");
    for (const specifier of runtimeImportSpecifiers(source, absolutePath)) {
      const dependency = resolveLocalModule(absolutePath, specifier, rootDir);
      if (dependency) visit(dependency);
    }
  };
  visit(absoluteEntry);
  return {
    modules,
    stylesheets,
  };
};

export const selectReachableOwnerStylesheets = ({
  reachableStylesheets,
  ownerFiles,
  indexFile,
}) => {
  const reachable = new Set(reachableStylesheets.map((file) => path.resolve(file)));
  const indexPath = path.resolve(indexFile);
  if (!reachable.has(indexPath)) {
    throw new Error(`Runtime CSS graph does not reach ${indexPath}`);
  }
  const ownerPaths = ownerFiles.map((file) => path.resolve(file));
  const missingOwners = ownerPaths.filter((ownerPath) => !reachable.has(ownerPath));
  if (missingOwners.length > 0) {
    throw new Error(
      `Runtime CSS graph omits owner stylesheets:\n${missingOwners.join("\n")}`,
    );
  }
  return [indexPath, ...ownerPaths];
};

export const TARGET_STATE_MARKERS = [
  "hover",
  "focus-visible",
  "active",
  "checked",
  "first-child",
  "last-child",
].map((state) => `__cascade-force-${state}`);

const PRESERVED_PSEUDO_CLASSES = new Set([
  "has",
  "is",
  "not",
  "root",
  "where",
]);

const uniqueStrings = (values) => [...new Set(values)];

const isWithinKeyframes = (rule) => {
  for (let parent = rule.parent; parent; parent = parent.parent) {
    if (parent.type === "atrule" && /keyframes$/i.test(parent.name)) return true;
  }
  return false;
};

const markerForPseudoClass = (node) => {
  const simpleName = node.name.toLowerCase();
  if (!node.children) return `__cascade-force-${simpleName}`;
  const digest = crypto
    .createHash("sha256")
    .update(csstree.generate(node))
    .digest("hex")
    .slice(0, 10);
  return `__cascade-force-${simpleName}-${digest}`;
};

export const transformSelector = (
  selector,
  { stripPseudoElements = false } = {},
) => {
  const ast = csstree.parse(selector, { context: "selectorList" });
  csstree.walk(ast, (node, item, list) => {
    if (
      node.type === "PseudoClassSelector" &&
      !PRESERVED_PSEUDO_CLASSES.has(node.name.toLowerCase())
    ) {
      item.data = {
        type: "ClassSelector",
        loc: null,
        name: markerForPseudoClass(node),
      };
    } else if (node.type === "PseudoElementSelector" && stripPseudoElements) {
      list.remove(item);
    }
  });
  const result = csstree.generate(ast).trim();
  return result || "*";
};

export const forcePseudoStates = (css) => {
  const root = postcss.parse(css);
  root.walkRules((rule) => {
    if (!isWithinKeyframes(rule)) rule.selector = transformSelector(rule.selector);
  });
  return root.toString();
};

const attributeName = (node) => {
  const name = csstree.generate(node.name);
  if (!/^[a-zA-Z_][a-zA-Z0-9_.:-]*$/.test(name)) {
    throw new Error(`unsupported attribute name ${name}`);
  }
  return name;
};

const attributeValue = (node) => {
  if (!node.value) return "";
  const value =
    node.value.type === "String"
      ? node.value.value
      : node.value.type === "Identifier"
        ? node.value.name
        : csstree.generate(node.value);
  if (!node.matcher || ["=", "~=", "|="].includes(node.matcher)) return value;
  if (node.matcher === "^=") return `${value}-suffix`;
  if (node.matcher === "$=") return `prefix-${value}`;
  if (node.matcher === "*=") return `prefix-${value}-suffix`;
  throw new Error(`unsupported attribute matcher ${node.matcher}`);
};

const emptyNode = () => ({
  tag: "div",
  id: undefined,
  classNames: [],
  attributes: {},
  requiredChildren: [],
});

const selectorAlternatives = (node) => {
  const selectorList = node.children?.toArray().find(
    (child) => child.type === "SelectorList",
  );
  return selectorList?.children.toArray() ?? [];
};

const applySimpleSelector = (simple, descriptor) => {
  if (simple.type === "ClassSelector") {
    descriptor.classNames.push(simple.name);
    return;
  }
  if (simple.type === "IdSelector") {
    if (descriptor.id && descriptor.id !== simple.name) {
      throw new Error(`conflicting ids #${descriptor.id} and #${simple.name}`);
    }
    descriptor.id = simple.name;
    return;
  }
  if (simple.type === "TypeSelector") {
    if (simple.name === "*") return;
    if (!/^[a-zA-Z][a-zA-Z0-9-]*$/.test(simple.name)) {
      throw new Error(`unsupported type selector ${simple.name}`);
    }
    if (descriptor.tag !== "div" && descriptor.tag !== simple.name) {
      throw new Error(
        `conflicting type selectors ${descriptor.tag} and ${simple.name}`,
      );
    }
    descriptor.tag = simple.name;
    return;
  }
  if (simple.type === "AttributeSelector") {
    descriptor.attributes[attributeName(simple)] = attributeValue(simple);
    return;
  }
  if (simple.type === "PseudoClassSelector") {
    const name = simple.name.toLowerCase();
    if (name === "not") return;
    if (name === "is" || name === "where") {
      const alternatives = selectorAlternatives(simple);
      if (alternatives.length !== 1) {
        throw new Error(`:${name}() with multiple alternatives is unsupported`);
      }
      const children = alternatives[0].children.toArray();
      if (children.some((child) => child.type === "Combinator")) {
        throw new Error(`complex :${name}() is unsupported`);
      }
      for (const child of children) applySimpleSelector(child, descriptor);
      return;
    }
    if (name === "has") {
      const alternatives = selectorAlternatives(simple);
      if (alternatives.length !== 1) {
        throw new Error(":has() with multiple alternatives is unsupported");
      }
      const children = alternatives[0].children.toArray();
      if (children.some((child) => child.type === "Combinator")) {
        throw new Error("complex :has() is unsupported");
      }
      const requiredChild = emptyNode();
      for (const child of children) applySimpleSelector(child, requiredChild);
      descriptor.requiredChildren.push(requiredChild);
      return;
    }
    if (name === "root") {
      throw new Error(":root is document-scoped and cannot be isolated");
    }
    throw new Error(`unforced pseudo-class :${name}`);
  }
  throw new Error(`unsupported selector node ${simple.type}`);
};

const selectorStructure = (selector) => {
  const ast = csstree.parse(selector, { context: "selector" });
  const nodes = [emptyNode()];
  const combinators = [];
  for (const child of ast.children.toArray()) {
    if (child.type === "Combinator") {
      if (![" ", ">", "+", "~"].includes(child.name)) {
        throw new Error(`unsupported combinator ${child.name}`);
      }
      combinators.push(child.name);
      nodes.push(emptyNode());
      continue;
    }
    applySimpleSelector(child, nodes.at(-1));
  }
  if (combinators.length !== nodes.length - 1) {
    throw new Error("selector has an empty compound");
  }
  for (const node of nodes) node.classNames = uniqueStrings(node.classNames);
  return { nodes, combinators };
};

const pseudoElements = (selector) => {
  const result = [];
  const ast = csstree.parse(selector, { context: "selector" });
  csstree.walk(ast, (node) => {
    if (node.type !== "PseudoElementSelector") return;
    if (node.children) {
      throw new Error(
        `functional pseudo-element ${csstree.generate(node)} is unsupported`,
      );
    }
    result.push(`::${node.name}`);
  });
  return uniqueStrings(result);
};

export const extractCssSelectorScenarios = (css) => {
  const root = postcss.parse(forcePseudoStates(css));
  const branchOccurrences = [];
  root.walkRules((rule) => {
    if (isWithinKeyframes(rule)) return;
    const selectorList = csstree.parse(rule.selector, {
      context: "selectorList",
    });
    for (const selector of selectorList.children.toArray()) {
      branchOccurrences.push({
        selector: csstree.generate(selector),
        line: rule.source?.start?.line,
      });
    }
  });

  const uniqueBranches = new Map();
  for (const branch of branchOccurrences) {
    if (!uniqueBranches.has(branch.selector)) {
      uniqueBranches.set(branch.selector, branch);
    }
  }

  const scenarios = [];
  const unsupported = [];
  for (const branch of uniqueBranches.values()) {
    try {
      const matchSelector = transformSelector(branch.selector, {
        stripPseudoElements: true,
      });
      scenarios.push({
        kind: "css-selector",
        source: `css:${branch.line ?? "?"}`,
        selector: branch.selector,
        matchSelector,
        ...selectorStructure(matchSelector),
        pseudoElements: pseudoElements(branch.selector),
      });
    } catch (error) {
      unsupported.push({
        selector: branch.selector,
        line: branch.line,
        reason: error instanceof Error ? error.message : String(error),
      });
    }
  }

  return {
    scenarios,
    unsupported,
    branchOccurrenceCount: branchOccurrences.length,
    uniqueBranchCount: uniqueBranches.size,
  };
};

const realClassNames = (node) =>
  node.classNames.filter(
    (className) => !className.startsWith("__cascade-force-"),
  );

const matchesTsxTarget = (cssScenario, tsxScenario) => {
  const cssTargetClasses = realClassNames(cssScenario.nodes.at(-1));
  const tsxTargetClasses = new Set(tsxScenario.nodes.at(-1).classNames);
  return cssTargetClasses.every((className) =>
    tsxTargetClasses.has(className),
  );
};

const matchesTsxStructure = (cssScenario, tsxScenario) => {
  if (cssScenario.combinators.some((combinator) => ["+", "~"].includes(combinator))) {
    return false;
  }
  if (!matchesTsxTarget(cssScenario, tsxScenario)) return false;
  const tsxAncestors = tsxScenario.nodes.slice(0, -1);
  let searchFrom = 0;
  for (const cssAncestor of cssScenario.nodes.slice(0, -1)) {
    const requiredClasses = realClassNames(cssAncestor);
    if (requiredClasses.length === 0) continue;
    const matchIndex = tsxAncestors.findIndex(
      (tsxAncestor, index) =>
        index >= searchFrom &&
        requiredClasses.every((className) =>
          tsxAncestor.classNames.includes(className),
        ),
    );
    if (matchIndex < 0) return false;
    searchFrom = matchIndex + 1;
  }
  return true;
};

export const enrichCssScenariosFromTsx = (cssScenarios, tsxScenarios) =>
  cssScenarios.map((scenario) => {
    const contextualMatches = tsxScenarios.filter((tsxScenario) =>
      matchesTsxStructure(scenario, tsxScenario),
    );
    // Cross-component ancestry cannot be recovered from one TSX source file.
    // In that case, retain only same-element classes shared by every matching
    // TSX target; the synthetic CSS structure still owns the ancestor chain.
    const targetMatches = tsxScenarios.filter((tsxScenario) =>
      matchesTsxTarget(scenario, tsxScenario),
    );
    const matches =
      contextualMatches.length > 0 ? contextualMatches : targetMatches;
    if (matches.length === 0) return scenario;
    const classSets = matches.map(
      (tsxScenario) => new Set(tsxScenario.nodes.at(-1).classNames),
    );
    const impliedTargetClasses = [...classSets[0]].filter((className) =>
      classSets.every((classSet) => classSet.has(className)),
    );
    const nodes = cloneNodes(scenario.nodes);
    nodes.at(-1).classNames = uniqueStrings([
      ...nodes.at(-1).classNames,
      ...impliedTargetClasses,
    ]);
    return {
      ...scenario,
      nodes,
      tsxEnrichmentMode:
        contextualMatches.length > 0 ? "contextual" : "target-only",
      tsxEnrichmentMatchCount: matches.length,
      tsxImpliedTargetClasses: impliedTargetClasses.filter(
        (className) => !scenario.nodes.at(-1).classNames.includes(className),
      ),
    };
  });

const cloneNodes = (nodes) =>
  nodes.map((node) => ({
    ...node,
    classNames: [...node.classNames],
    attributes: { ...node.attributes },
    requiredChildren: cloneNodes(node.requiredChildren),
  }));

export const buildFixtureDescriptors = (scenarios) => {
  const fixtures = [];
  const targets = [];
  scenarios.forEach((scenario, scenarioIndex) => {
    const variants =
      scenario.kind === "tsx"
        ? [
            [],
            TARGET_STATE_MARKERS,
          ]
        : [[]];
    variants.forEach((states, variantIndex) => {
      const nodes = cloneNodes(scenario.nodes);
      nodes.at(-1).classNames = uniqueStrings([
        ...nodes.at(-1).classNames,
        ...states,
      ]);
      const targetIndex = targets.length;
      const pseudos = uniqueStrings([
        "",
        ...(scenario.kind === "css-selector"
          ? (scenario.pseudoElements ?? [])
          : []),
      ]);
      targets.push({
        scenarioIndex,
        variantIndex,
        states,
        source: scenario.source,
        selector: scenario.selector,
        tag: nodes.at(-1).tag,
        classNames: nodes.at(-1).classNames,
        pseudos,
      });
      fixtures.push({
        scenarioIndex,
        targetIndex,
        nodes,
        combinators: scenario.combinators,
        matchSelector:
          scenario.kind === "css-selector" ? scenario.matchSelector : undefined,
      });
    });
  });
  return { fixtures, targets };
};

export const assertIndexedEntries = (entries, expectedCount, label) => {
  if (!Array.isArray(entries)) throw new Error(`${label} snapshot is not an array`);
  if (entries.length !== expectedCount) {
    throw new Error(
      `${label} snapshot target count ${entries.length} != ${expectedCount}`,
    );
  }
  const seen = new Set();
  entries.forEach((entry, index) => {
    if (!Number.isInteger(entry.targetIndex)) {
      throw new Error(`${label} snapshot entry ${index} has no integer index`);
    }
    if (seen.has(entry.targetIndex)) {
      throw new Error(`${label} snapshot repeats target ${entry.targetIndex}`);
    }
    seen.add(entry.targetIndex);
    if (entry.targetIndex !== index) {
      throw new Error(
        `${label} snapshot entry ${index} reports target ${entry.targetIndex}`,
      );
    }
  });
};

export const mergeComputedPropertyNames = (...propertyLists) =>
  uniqueStrings(propertyLists.flat()).sort();
