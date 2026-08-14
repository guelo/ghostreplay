import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type Locator, type Page, type TestInfo } from "@playwright/test";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const OUTPUT_DIR = path.resolve(__dirname, "output");

const STABLE_SCREENSHOT_SAMPLES = 3;
const STABLE_SCREENSHOT_MAX_ATTEMPTS = 50;
const STABLE_SCREENSHOT_INTERVAL_MS = 100;

/**
 * Viewports chosen to exercise the app's real breakpoints
 * (legacy styles <=720 / <=767 / <=760h / <=680h / <=1099 /
 * max-height:640), not generic device buckets. Pages with no responsive
 * variance in a band reuse a single capture via the per-page subsets exposed
 * by {@link viewportsFor}.
 */
export interface Viewport {
  name: string;
  width: number;
  height: number;
}

export const VIEWPORTS: Record<string, Viewport> = {
  mobile: { name: "mobile", width: 390, height: 844 },
  mobileShort: { name: "mobileShort", width: 360, height: 640 },
  mixed: { name: "mixed", width: 750, height: 900 },
  tablet: { name: "tablet", width: 820, height: 1180 },
  desktopShort: { name: "desktopShort", width: 1024, height: 640 },
  desktop: { name: "desktop", width: 1440, height: 900 },
};

const ALL_VIEWPORTS = Object.values(VIEWPORTS);

/**
 * Per-page viewport subsets. Auth/landing pages have a single column layout
 * with no breakpoint variance worth re-shooting, so they only need a couple of
 * bands. Game/analysis pages span the full set because the mixed (750) and
 * short-height (1024x640) bands drive distinct layouts.
 */
const VIEWPORT_SUBSETS: Record<string, Viewport[]> = {
  landing: [VIEWPORTS.mobile, VIEWPORTS.desktop],
  auth: [VIEWPORTS.mobile, VIEWPORTS.desktop],
};

export const viewportsFor = (page: string): Viewport[] =>
  VIEWPORT_SUBSETS[page] ?? ALL_VIEWPORTS;

/** Fixed instant so due-dates, stats windows, and the rating graph render deterministically. */
export const FIXED_TIME = new Date("2026-06-01T12:00:00.000Z");

export const freezeClock = async (page: Page): Promise<void> => {
  await page.clock.install({ time: FIXED_TIME });
  await page.clock.setFixedTime(FIXED_TIME);
};

/** Kill animations/transitions before capture so screenshots are stable. */
export const killAnimations = async (page: Page): Promise<void> => {
  await page.addInitScript(() => {
    const style = document.createElement("style");
    style.textContent = `*, *::before, *::after {
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
      caret-color: transparent !important;
    }`;
    const attach = () => document.head?.appendChild(style);
    if (document.head) attach();
    else document.addEventListener("DOMContentLoaded", attach);
  });
};

/** Pin client-side sampling while leaving production rendering untouched. */
export const freezeRandom = async (page: Page): Promise<void> => {
  await page.addInitScript(() => {
    Math.random = () => 0.5;
  });
};

/** Apply determinism controls to a fresh page (call before navigation). */
export const prepareDeterministicPage = async (page: Page): Promise<void> => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await freezeClock(page);
  await killAnimations(page);
  await freezeRandom(page);
};

/**
 * Replace only AnalysisBoard's visible Stockfish worker with fixed output.
 *
 * The live-game analysis worker remains real. This mock exists so the gallery
 * can render the engine panel's production CSS (including depth, progress,
 * populated lines, and placeholders) without making pixels depend on local
 * Stockfish search timing.
 */
export const installDeterministicStockfish = async (
  page: Page,
): Promise<void> => {
  await page.addInitScript(() => {
    const NativeWorker = window.Worker;

    class GalleryStockfishWorker extends EventTarget {
      onerror: ((this: Worker, ev: ErrorEvent) => unknown) | null = null;
      onmessage: ((this: Worker, ev: MessageEvent) => unknown) | null = null;
      onmessageerror: ((this: Worker, ev: MessageEvent) => unknown) | null =
        null;

      constructor() {
        super();
        queueMicrotask(() => {
          this.emit({ type: "booted" });
          this.emit({ type: "ready" });
        });
      }

      private emit(data: unknown): void {
        const event = new MessageEvent("message", { data });
        this.dispatchEvent(event);
        this.onmessage?.call(this as unknown as Worker, event);
      }

      postMessage(message: unknown): void {
        const request = message as {
          type?: string;
          id?: string;
          fen?: string;
        };
        if (
          request.type !== "evaluate-position" ||
          !request.id ||
          !request.fen
        ) {
          return;
        }

        queueMicrotask(() => {
          this.emit({
            type: "thinking",
            id: request.id,
            fen: request.fen,
          });
          this.emit({
            type: "info",
            id: request.id,
            info: {
              depth: 21,
              multipv: 1,
              pv: ["b8c6"],
              score: { type: "cp", value: 34 },
            },
            raw: "info depth 21 multipv 1 score cp 34 pv b8c6",
          });
        });
      }

      terminate(): void {}
    }

    window.Worker = new Proxy(NativeWorker, {
      construct(target, args, newTarget) {
        if (String(args[0]).includes("stockfishWorker")) {
          return new GalleryStockfishWorker() as unknown as Worker;
        }
        return Reflect.construct(target, args, newTarget) as Worker;
      },
    });
  });
};

// --- Gallery registry ----------------------------------------------------

interface Shot {
  page: string;
  state: string;
  viewport: string;
  file: string;
}

const shots: Shot[] = [];

/**
 * Wipe the output dir once at the start of a run so stale PNGs from a previous
 * (possibly --grep-filtered) run don't linger in the gallery. Guarded so it
 * only fires on the first capture of the process.
 */
let outputReset = false;
const resetOutputDirOnce = (): void => {
  if (outputReset) return;
  outputReset = true;
  fs.rmSync(OUTPUT_DIR, { recursive: true, force: true });
};

interface CaptureOptions {
  /** Logical page key, e.g. "history". */
  page: string;
  /** UI state, e.g. "empty". */
  state: string;
  viewport: Viewport;
  /** Locator that must be visible before the screenshot is taken. */
  waitFor?: Locator;
  /** Capture the full scrollable page instead of the viewport. */
  fullPage?: boolean;
  /**
   * Skip the viewport resize before capture. Use when the caller has already
   * sized the page AND established viewport-dependent state (e.g. a re-measured
   * overlay) that a redundant resize would invalidate.
   */
  skipResize?: boolean;
}

const stabilizeAnalysisScroll = (pw: Page): Promise<void> =>
  pw.locator(".analysis-board:visible").evaluateAll((boards) => {
    for (const board of boards) {
      const vertical = board.querySelector<HTMLElement>(".move-list-scroll");
      const verticalSelected = vertical?.querySelector<HTMLElement>(
        ".move-button.selected",
      );
      if (vertical && verticalSelected) {
        vertical.scrollTop = Math.max(
          0,
          verticalSelected.offsetTop -
            (vertical.clientHeight - verticalSelected.offsetHeight) / 2,
        );
      }

      const horizontal = board.querySelector<HTMLElement>(
        ".h-move-list__strip",
      );
      const horizontalSelected = horizontal?.querySelector<HTMLElement>(
        ".h-move.selected",
      )?.parentElement;
      if (horizontal && horizontalSelected) {
        horizontal.scrollLeft = Math.max(
          0,
          horizontalSelected.offsetLeft -
            (horizontal.clientWidth - horizontalSelected.offsetWidth) / 2,
        );
      }
    }
  });

/**
 * Screenshot a page/state/viewport into the gallery output dir AND attach it to
 * the Playwright HTML report (raw path screenshots do not auto-appear there).
 */
export const captureState = async (
  pw: Page,
  testInfo: TestInfo,
  opts: CaptureOptions,
): Promise<void> => {
  const { page, state, viewport, waitFor, fullPage, skipResize } = opts;
  resetOutputDirOnce();
  const currentViewport = pw.viewportSize();
  if (
    !skipResize &&
    (currentViewport?.width !== viewport.width ||
      currentViewport.height !== viewport.height)
  ) {
    await pw.setViewportSize({ width: viewport.width, height: viewport.height });
  }
  if (waitFor) {
    // .first() so a state locator that legitimately matches multiple nodes
    // (e.g. several .stats-section blocks) doesn't trip strict mode.
    await expect(waitFor.first()).toBeVisible({ timeout: 30_000 });
  }
  // Responsive analysis layouts swap between vertical and horizontal move
  // lists. Their selected-move effects can race the screenshot after a resize,
  // so place the selected token at a deterministic scroll offset ourselves.
  await stabilizeAnalysisScroll(pw);
  // A resize can leave the process-local pointer over a different control and
  // introduce a hover/focus fragment at the viewport edge. Park it outside the
  // app's interactive content before sampling.
  await pw.mouse.move(0, 0);
  // Settle layout/fonts after the viewport change.
  await pw.evaluate(() => document.fonts?.ready);

  const fileName = `${page}-${state}.png`;
  const relPath = path.join(viewport.name, fileName);
  const absPath = path.join(OUTPUT_DIR, relPath);
  fs.mkdirSync(path.dirname(absPath), { recursive: true });
  let previous: Buffer | null = null;
  let identicalSamples = 0;
  let buffer: Buffer | null = null;
  for (let attempt = 0; attempt < STABLE_SCREENSHOT_MAX_ATTEMPTS; attempt += 1) {
    // React's selected-move effect can run after the initial resize settle.
    // Reassert immediately before every sample so it cannot win that race.
    await stabilizeAnalysisScroll(pw);
    const candidate = await pw.screenshot({
      animations: "disabled",
      fullPage,
    });
    identicalSamples = previous?.equals(candidate)
      ? identicalSamples + 1
      : 1;
    previous = candidate;

    if (identicalSamples >= STABLE_SCREENSHOT_SAMPLES) {
      buffer = candidate;
      break;
    }
    await pw.waitForTimeout(STABLE_SCREENSHOT_INTERVAL_MS);
  }
  if (!buffer) {
    throw new Error(
      `Screenshot did not settle: ${page}/${viewport.name}/${state}`,
    );
  }
  fs.writeFileSync(absPath, buffer);
  await testInfo.attach(`${page}/${viewport.name}/${state}`, {
    body: buffer,
    contentType: "image/png",
  });

  shots.push({ page, state, viewport: viewport.name, file: relPath });
};

/** Write the human-review contact sheet. Call ONCE from a final afterAll. */
export const buildIndex = (): void => {
  if (shots.length === 0) return;

  const byPage = new Map<string, Map<string, Shot[]>>();
  for (const shot of shots) {
    const states = byPage.get(shot.page) ?? new Map<string, Shot[]>();
    const group = states.get(shot.state) ?? [];
    group.push(shot);
    states.set(shot.state, group);
    byPage.set(shot.page, states);
  }

  const escape = (value: string): string =>
    value.replace(/[&<>"]/g, (c) =>
      c === "&" ? "&amp;" : c === "<" ? "&lt;" : c === ">" ? "&gt;" : "&quot;",
    );

  const sections: string[] = [];
  for (const [pageName, states] of [...byPage.entries()].sort()) {
    const stateBlocks: string[] = [];
    for (const [stateName, group] of [...states.entries()].sort()) {
      const cells = group
        .sort((a, b) => a.viewport.localeCompare(b.viewport))
        .map(
          (shot) => `
        <figure class="cell">
          <a href="${escape(shot.file)}" target="_blank">
            <img loading="lazy" src="${escape(shot.file)}" alt="${escape(
              `${pageName} ${stateName} ${shot.viewport}`,
            )}" />
          </a>
          <figcaption>${escape(shot.viewport)}</figcaption>
        </figure>`,
        )
        .join("");
      stateBlocks.push(`
      <div class="state">
        <h3>${escape(stateName)}</h3>
        <div class="row">${cells}</div>
      </div>`);
    }
    sections.push(`
    <section class="page">
      <h2>${escape(pageName)}</h2>
      ${stateBlocks.join("")}
    </section>`);
  }

  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Ghost Replay — Screenshot Gallery</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #14161a; color: #e8eaed; }
  header { padding: 16px 24px; border-bottom: 1px solid #2a2d33; position: sticky; top: 0; background: #14161a; }
  h1 { font-size: 18px; margin: 0; }
  .meta { color: #9aa0a6; font-size: 13px; margin-top: 4px; }
  .page { padding: 16px 24px; border-bottom: 1px solid #2a2d33; }
  .page > h2 { font-size: 16px; text-transform: capitalize; }
  .state > h3 { font-size: 13px; color: #9aa0a6; font-weight: 600; margin: 16px 0 8px; }
  .row { display: flex; flex-wrap: wrap; gap: 16px; }
  .cell { margin: 0; }
  .cell img { max-height: 260px; max-width: 360px; border: 1px solid #2a2d33; border-radius: 6px; background: #fff; display: block; }
  figcaption { font-size: 11px; color: #9aa0a6; margin-top: 4px; text-align: center; }
</style>
</head>
<body>
<header>
  <h1>Ghost Replay — Screenshot Gallery</h1>
  <div class="meta">${shots.length} screenshots · generated ${new Date().toISOString()}</div>
</header>
${sections.join("")}
</body>
</html>`;

  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  fs.writeFileSync(path.join(OUTPUT_DIR, "index.html"), html, "utf8");
};

/** Route helper: fail an endpoint with a non-retryable error to force error UI. */
export const failRoute = (
  page: Page,
  pattern: string,
  status = 500,
  body: unknown = { detail: "Forced e2e error" },
): Promise<void> =>
  page.route(pattern, (route) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    }),
  );

/** Route helper: hold a request open so the loading state stays visible. */
export const stallRoute = (page: Page, pattern: string): Promise<void> =>
  page.route(pattern, () => {
    /* never fulfilled — request hangs, loading UI persists */
  });
