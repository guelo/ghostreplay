import { ReactElement, ReactNode } from 'react'
import { render, RenderOptions } from '@testing-library/react'

interface WrapperProps {
  children: ReactNode
}

interface CustomRenderOptions extends Omit<RenderOptions, 'wrapper'> {
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

// Re-export everything from React Testing Library
export * from '@testing-library/react'

// Override render method
export { customRender as render }
