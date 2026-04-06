import { expect, test } from '@playwright/test'

test('stockfish wrapper worker boots to ready in the browser', async ({ page }) => {
  await page.goto('/')

  const result = await page.evaluate(async () => {
    return await new Promise<{
      messages: Array<{ type: string; line?: string; error?: string; message?: string }>
    }>((resolve) => {
      const messages: Array<{ type: string; line?: string; error?: string; message?: string }> = []
      const worker = new Worker('/src/workers/stockfishWorker.ts?worker_file&type=module', {
        type: 'module',
      })

      const finish = () => {
        worker.removeEventListener('message', handleMessage)
        worker.removeEventListener('error', handleError)
        worker.terminate()
        resolve({ messages })
      }

      const timeoutId = window.setTimeout(() => {
        messages.push({ type: 'timeout' })
        finish()
      }, 5000)

      const handleMessage = (event: MessageEvent) => {
        const data = event.data as { type?: string; line?: string; error?: string }
        messages.push({
          type: data.type ?? 'unknown',
          line: data.line,
          error: data.error,
        })

        if (data.type === 'ready' || data.type === 'error') {
          window.clearTimeout(timeoutId)
          finish()
        }
      }

      const handleError = (event: ErrorEvent) => {
        window.clearTimeout(timeoutId)
        messages.push({ type: 'error-event', message: event.message })
        finish()
      }

      worker.addEventListener('message', handleMessage)
      worker.addEventListener('error', handleError)
    })
  })

  expect(result.messages.some((message) => message.type === 'booted')).toBe(true)
  expect(result.messages.some((message) => message.type === 'ready')).toBe(true)
  expect(result.messages.some((message) => message.type === 'error')).toBe(false)
  expect(result.messages.some((message) => message.type === 'error-event')).toBe(false)
  expect(result.messages.some((message) => message.type === 'timeout')).toBe(false)
})
