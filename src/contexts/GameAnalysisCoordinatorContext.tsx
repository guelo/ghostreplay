import { createContext, useContext, useEffect } from 'react'
import type { ReactNode } from 'react'
import {
  GameAnalysisCoordinator,
  gameAnalysisCoordinator,
} from '../services/GameAnalysisCoordinator'

const CoordinatorContext = createContext<GameAnalysisCoordinator | null>(null)

export function GameAnalysisCoordinatorProvider({ children }: { children: ReactNode }) {
  useEffect(() => {
    return () => {
      // App unmount — clean up timers but don't destroy the singleton
      // (StrictMode double-mounts would kill it otherwise)
    }
  }, [])

  return (
    <CoordinatorContext.Provider value={gameAnalysisCoordinator}>
      {children}
    </CoordinatorContext.Provider>
  )
}

export function useGameAnalysisCoordinator(): GameAnalysisCoordinator {
  const coordinator = useContext(CoordinatorContext)
  if (!coordinator) {
    throw new Error('useGameAnalysisCoordinator used outside GameAnalysisCoordinatorProvider')
  }
  return coordinator
}
