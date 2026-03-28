import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import AppRoutes from './AppRoutes.tsx'
import { AuthProvider } from './contexts/AuthContext.tsx'
import { GameAnalysisCoordinatorProvider } from './contexts/GameAnalysisCoordinatorContext.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthProvider>
      <BrowserRouter>
        <GameAnalysisCoordinatorProvider>
          <AppRoutes />
        </GameAnalysisCoordinatorProvider>
      </BrowserRouter>
    </AuthProvider>
  </StrictMode>,
)
