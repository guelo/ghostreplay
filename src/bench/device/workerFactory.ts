/**
 * Construct the analysis worker EXACTLY as production does.
 *
 * This one line is the whole premise of the device runner (g-two-search-grade
 * §10.1): the runner must exercise the shipping worker's own orchestration —
 * one reset, the shared budget, the MultiPV sequence, deadline stop/grace, and
 * the heartbeat — or the numbers describe the harness instead of the product.
 *
 * `workerParity.test.ts` pins this call against both production call sites
 * (`GameAnalysisCoordinator.ensureWorker` and `useMoveAnalysis`). If production
 * ever changes how it builds the worker, that test fails and this file must
 * follow — the instrument is not allowed to drift from the thing it measures.
 */
export const createAnalysisWorker = (): Worker =>
  new Worker(new URL('../../workers/analysisWorker.ts', import.meta.url), {
    type: 'module',
  })
