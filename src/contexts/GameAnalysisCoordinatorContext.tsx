import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { gameAnalysisCoordinator } from '../services/GameAnalysisCoordinator'
import { CoordinatorContext } from './useGameAnalysisCoordinator'

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
