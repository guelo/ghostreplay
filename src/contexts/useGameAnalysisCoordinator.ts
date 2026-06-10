import { createContext, useContext } from 'react'
import type { GameAnalysisCoordinator } from '../services/GameAnalysisCoordinator'

export const CoordinatorContext = createContext<GameAnalysisCoordinator | null>(null)

export function useGameAnalysisCoordinator(): GameAnalysisCoordinator {
  const coordinator = useContext(CoordinatorContext)
  if (!coordinator) {
    throw new Error('useGameAnalysisCoordinator used outside GameAnalysisCoordinatorProvider')
  }
  return coordinator
}
