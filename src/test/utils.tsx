import type { ReactElement, ReactNode } from 'react'
import {
  render,
  type RenderOptions,
  act,
  fireEvent,
  screen,
  waitFor,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'

interface WrapperProps {
  children: ReactNode
}

type CustomRenderOptions = Omit<RenderOptions, 'wrapper'> & {
  // Add custom options here as needed
  // For example, when auth context is added:
  // authState?: Partial<AuthState>
  // gameState?: Partial<GameState>
}

function createWrapper(): React.FC<WrapperProps> {
  return function Wrapper({ children }: WrapperProps) {
    // When contexts are added, wrap children with providers here
    // Example:
    // return (
    //   <AuthProvider initialState={authState}>
    //     <GameProvider initialState={gameState}>
    //       {children}
    //     </GameProvider>
    //   </AuthProvider>
    // )
    return <>{children}</>
  }
}

function customRender(
  ui: ReactElement,
  options: CustomRenderOptions = {},
) {
  const Wrapper = createWrapper()

  return render(ui, { wrapper: Wrapper, ...options })
}

// Override render method
export { customRender as render }
export { act, fireEvent, screen, userEvent, waitFor }
