import { getStatsActivity } from '../utils/api'

const positiveUserIds = new Set<number>()
const inFlightByUserId = new Map<number, Promise<boolean>>()

export const hasCachedHomeActivity = (userId: number): boolean =>
  positiveUserIds.has(userId)

export const resolveHomeActivity = (userId: number): Promise<boolean> => {
  if (positiveUserIds.has(userId)) {
    return Promise.resolve(true)
  }

  const existing = inFlightByUserId.get(userId)
  if (existing) {
    return existing
  }

  const request = getStatsActivity()
    .then(({ has_game_or_drill: hasGameOrDrill }) => {
      if (hasGameOrDrill) {
        positiveUserIds.add(userId)
      }
      return hasGameOrDrill
    })
    .finally(() => {
      if (inFlightByUserId.get(userId) === request) {
        inFlightByUserId.delete(userId)
      }
    })

  inFlightByUserId.set(userId, request)
  return request
}

/** Reset page-lifetime module state between tests only. */
export const __resetHomeActivityForTests = (): void => {
  positiveUserIds.clear()
  inFlightByUserId.clear()
}
