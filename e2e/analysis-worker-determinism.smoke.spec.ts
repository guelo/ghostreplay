import { expect, test } from '@playwright/test'

/**
 * AC #10 (g-deterministic-grade): browser-fallback analysis must be
 * reproducible. The analysisWorker reuses a long-lived Stockfish across
 * independent positions, so without a per-request `ucinewgame`+`isready` reset
 * the same FEN can score differently depending on what was searched before it.
 *
 * This drives the REAL worker in Chromium: analyze position A, analyze a
 * different position B in between (to dirty the engine), then analyze A again.
 * The two A results must be identical. Mocked unit tests only prove command
 * ordering — this is the only check that proves reproducibility.
 */
test('analysisWorker returns identical results for a repeated FEN across an intervening position', async ({
  page,
}) => {
  // Three depth-17 single-threaded analyses (each up to 3 searches) — give it room.
  test.setTimeout(90_000)

  await page.goto('/')
  await expect(page.locator('.home-hero')).toBeVisible()

  const FEN_A = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
  const MOVE_A = 'e2e4'
  // A distinct middlegame position + move to dirty the shared engine state.
  const FEN_B = 'r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3'
  const MOVE_B = 'f1c4'

  const analyses = await page.evaluate(
    async ({ FEN_A, MOVE_A, FEN_B, MOVE_B }) => {
      type AnalysisMsg = {
        type: string
        id?: string
        bestMove?: string
        bestLine?: string[]
        bestEval?: number | null
        playedEval?: number | null
        delta?: number | null
        classification?: string | null
        canonical?: boolean
        error?: string
      }

      const worker = new Worker(
        '/src/workers/analysisWorker.ts?worker_file&type=module',
        { type: 'module' },
      )

      const results: Record<string, AnalysisMsg> = {}

      const waitForReady = () =>
        new Promise<void>((resolve, reject) => {
          const onReady = (event: MessageEvent) => {
            const data = event.data as AnalysisMsg
            if (data.type === 'ready') {
              worker.removeEventListener('message', onReady)
              resolve()
            } else if (data.type === 'error' && data.id === undefined) {
              worker.removeEventListener('message', onReady)
              reject(new Error(data.error ?? 'worker error'))
            }
          }
          worker.addEventListener('message', onReady)
          setTimeout(() => reject(new Error('worker never became ready')), 20_000)
        })

      const analyze = (id: string, fen: string, move: string) =>
        new Promise<AnalysisMsg>((resolve, reject) => {
          const onMessage = (event: MessageEvent) => {
            const data = event.data as AnalysisMsg
            if (data.id !== id) return
            if (data.type === 'analysis') {
              worker.removeEventListener('message', onMessage)
              results[id] = data
              resolve(data)
            } else if (data.type === 'error') {
              worker.removeEventListener('message', onMessage)
              reject(new Error(data.error ?? 'analysis error'))
            }
          }
          worker.addEventListener('message', onMessage)
          worker.postMessage({ type: 'analyze-move', id, fen, move, playerColor: 'white' })
          setTimeout(() => reject(new Error(`analysis ${id} timed out`)), 25_000)
        })

      await waitForReady()

      // Serialize the three runs so the intervening position genuinely runs
      // between the two identical A analyses.
      const a1 = await analyze('a1', FEN_A, MOVE_A)
      await analyze('b1', FEN_B, MOVE_B)
      const a2 = await analyze('a2', FEN_A, MOVE_A)

      worker.terminate()
      return { a1, a2 }
    },
    { FEN_A, MOVE_A, FEN_B, MOVE_B },
  )

  const { a1, a2 } = analyses

  // Both A runs must agree on every grading-relevant field.
  expect(a2.bestMove).toBe(a1.bestMove)
  expect(a2.delta).toBe(a1.delta)
  expect(a2.bestEval).toBe(a1.bestEval)
  expect(a2.playedEval).toBe(a1.playedEval)
  expect(a2.classification).toBe(a1.classification)
  expect(a2.bestLine).toEqual(a1.bestLine)
  // The fallback path should produce canonical (win-chance model) results here.
  expect(a1.canonical).toBe(true)
  expect(a2.canonical).toBe(true)
})
