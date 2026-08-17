import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { PostHogProvider } from '@posthog/react'
import './index.css'
import './styles/app-shell.css'
import './styles/shared-controls.css'
import './styles/shared-effects.css'
import AppRoutes from './AppRoutes.tsx'
import { AuthProvider } from './contexts/AuthContext.tsx'
import { GameAnalysisCoordinatorProvider } from './contexts/GameAnalysisCoordinatorContext.tsx'
import DebugOverlay from './components/DebugOverlay.tsx'
import { installConsoleCapture } from './utils/debugLog.ts'
import { initAnalytics, posthog } from './analytics/posthog.ts'

installConsoleCapture()
// Initialize the PostHog singleton before render so autocapture/pageviews are
// active from the first paint and the provider receives an initialized client.
initAnalytics()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PostHogProvider client={posthog}>
      <AuthProvider>
        <BrowserRouter>
          <GameAnalysisCoordinatorProvider>
            <AppRoutes />
          </GameAnalysisCoordinatorProvider>
        </BrowserRouter>
      </AuthProvider>
      <DebugOverlay />
    </PostHogProvider>
  </StrictMode>,
)
