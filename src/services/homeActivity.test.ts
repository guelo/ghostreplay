import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  __resetHomeActivityForTests,
  hasCachedHomeActivity,
  resolveHomeActivity,
} from './homeActivity'

const getStatsActivityMock = vi.fn()

vi.mock('../utils/api', () => ({
  getStatsActivity: (...args: unknown[]) => getStatsActivityMock(...args),
}))

const deferred = <T,>() => {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('home activity resolver', () => {
  beforeEach(() => {
    getStatsActivityMock.mockReset()
    __resetHomeActivityForTests()
  })

  it('shares one in-flight request for the same user', async () => {
    const activity = deferred<{ has_game_or_drill: boolean }>()
    getStatsActivityMock.mockReturnValueOnce(activity.promise)

    const first = resolveHomeActivity(1)
    const second = resolveHomeActivity(1)

    expect(second).toBe(first)
    expect(getStatsActivityMock).toHaveBeenCalledTimes(1)

    activity.resolve({ has_game_or_drill: true })
    await expect(first).resolves.toBe(true)
    await expect(second).resolves.toBe(true)
  })

  it('reuses only positive results synchronously and isolates user IDs', async () => {
    getStatsActivityMock
      .mockResolvedValueOnce({ has_game_or_drill: true })
      .mockResolvedValueOnce({ has_game_or_drill: true })

    await expect(resolveHomeActivity(1)).resolves.toBe(true)
    expect(hasCachedHomeActivity(1)).toBe(true)
    expect(hasCachedHomeActivity(2)).toBe(false)

    await expect(resolveHomeActivity(1)).resolves.toBe(true)
    expect(getStatsActivityMock).toHaveBeenCalledTimes(1)

    await expect(resolveHomeActivity(2)).resolves.toBe(true)
    expect(getStatsActivityMock).toHaveBeenCalledTimes(2)
  })

  it('does not pin false results', async () => {
    getStatsActivityMock
      .mockResolvedValueOnce({ has_game_or_drill: false })
      .mockResolvedValueOnce({ has_game_or_drill: true })

    await expect(resolveHomeActivity(1)).resolves.toBe(false)
    expect(hasCachedHomeActivity(1)).toBe(false)
    await expect(resolveHomeActivity(1)).resolves.toBe(true)

    expect(getStatsActivityMock).toHaveBeenCalledTimes(2)
    expect(hasCachedHomeActivity(1)).toBe(true)
  })

  it('does not pin rejected requests', async () => {
    const failure = new Error('offline')
    getStatsActivityMock
      .mockRejectedValueOnce(failure)
      .mockResolvedValueOnce({ has_game_or_drill: false })

    await expect(resolveHomeActivity(1)).rejects.toBe(failure)
    await expect(resolveHomeActivity(1)).resolves.toBe(false)

    expect(getStatsActivityMock).toHaveBeenCalledTimes(2)
  })
})
