# Testing Setup

This directory contains test utilities and setup for the Ghost Replay application.

## Overview

The project uses:
- **Vitest** as the test runner
- **React Testing Library** for component testing
- **jsdom** for DOM simulation
- **@testing-library/jest-dom** for additional matchers

## Running Tests

```bash
# Run tests in watch mode
npm test

# Run tests once
npm run test:run

# Run tests with UI
npm run test:ui
```

## Test Utilities

### `src/test/utils.tsx`

Custom render function that wraps components with necessary providers (auth, game contexts, etc.).

```tsx
import { render, screen } from '@/test/utils'

describe('MyComponent', () => {
  it('should render', () => {
    render(<MyComponent />)
    expect(screen.getByText('Hello')).toBeInTheDocument()
  })
})
```

When auth and game contexts are implemented, the render function will automatically wrap components with those providers.

### `src/test/setup.ts`

Global test setup that:
- Configures jest-dom matchers
- Handles cleanup after each test
- Extends Vitest's expect with jest-dom assertions

## Writing Tests

### File Naming

Test files should be placed alongside the code they test or in this directory:
- `Component.test.tsx` - for components
- `hooks.test.ts` - for hooks
- `utils.test.ts` - for utilities

### Best Practices

1. Use the custom `render` from `@/test/utils` instead of RTL directly
2. Query by accessible roles and labels when possible
3. Avoid testing implementation details
4. Focus on user behavior and visible output

### Example Test

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen, userEvent } from '@/test/utils'
import { MyButton } from './MyButton'

describe('MyButton', () => {
  it('should call onClick when clicked', async () => {
    const handleClick = vi.fn()
    const user = userEvent.setup()

    render(<MyButton onClick={handleClick}>Click me</MyButton>)

    await user.click(screen.getByRole('button', { name: /click me/i }))

    expect(handleClick).toHaveBeenCalledTimes(1)
  })
})
```

## Future Enhancements

As the application grows, the test utilities will be extended to support:
- Mock auth state injection
- Mock game state injection
- Custom providers for different test scenarios
- Shared test fixtures and factories
