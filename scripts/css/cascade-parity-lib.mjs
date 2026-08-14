import crypto from "node:crypto";
import * as csstree from "css-tree";
import postcss from "postcss";

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
