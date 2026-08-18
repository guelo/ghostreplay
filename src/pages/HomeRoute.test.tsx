import {
  lazy,
  type ComponentType,
  type ReactElement,
} from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AUTH_STORAGE_KEYS } from '../contexts/authStorage'
import {
  __resetHomeActivityForTests,
  hasCachedHomeActivity,
} from '../services/homeActivity'
import { ApiError } from '../utils/api'
import HomeRoute from './HomeRoute'

const getStatsActivityMock = vi.fn()

vi.mock('../utils/api', async () => {
  const actual = await vi.importActual<typeof import('../utils/api')>(
    '../utils/api',
  )
  return {
    ...actual,
    getStatsActivity: (...args: unknown[]) => getStatsActivityMock(...args),
  }
})

type TestUser = {
  id: number
  username: string
  isAnonymous: boolean
}

let authState: { user: TestUser | null; isLoading: boolean }

vi.mock('../contexts/useAuth', () => ({
  useAuth: () => authState,
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

const marketingRender = vi.fn()
const dashboardRender = vi.fn()

function Marker({
  label,
  onRender,
}: {
  label: string
  onRender: () => void
}) {
  onRender()
  return <div data-testid={label}>{label}</div>
}

const homeElement = (): ReactElement => (
  <HomeRoute
    marketingElement={
      <Marker label="marketing" onRender={marketingRender} />
    }
    dashboardElement={
      <Marker label="dashboard" onRender={dashboardRender} />
    }
  />
)

const user = (id: number, isAnonymous: boolean): TestUser => ({
  id,
  username: `user-${id}`,
  isAnonymous,
})

describe('HomeRoute', () => {
  beforeEach(() => {
    getStatsActivityMock.mockReset()
    marketingRender.mockReset()
    dashboardRender.mockReset()
    __resetHomeActivityForTests()
    localStorage.clear()
    localStorage.setItem(AUTH_STORAGE_KEYS.token, 'stored-token')
    authState = { user: null, isLoading: false }
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('starts marketing immediately for a first visit and keeps registration out of the activity gate', () => {
    localStorage.clear()
    authState = { user: null, isLoading: true }
    const view = render(homeElement())

    expect(screen.getByTestId('marketing')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(getStatsActivityMock).not.toHaveBeenCalled()

    localStorage.setItem(AUTH_STORAGE_KEYS.credentials, 'new-credentials')
    localStorage.setItem(AUTH_STORAGE_KEYS.token, 'new-token')
    authState = { user: user(1, true), isLoading: false }
    view.rerender(homeElement())

    expect(screen.getByTestId('marketing')).toBeInTheDocument()
    expect(getStatsActivityMock).not.toHaveBeenCalled()
  })

  it.each([
    [AUTH_STORAGE_KEYS.token, 'stored-token'],
    [AUTH_STORAGE_KEYS.credentials, 'stored-credentials'],
  ])(
    'keeps the returning-user gate when only %s exists',
    async (storageKey, storageValue) => {
      localStorage.clear()
      localStorage.setItem(storageKey, storageValue)
      authState = { user: null, isLoading: true }
      getStatsActivityMock.mockResolvedValueOnce({
        has_game_or_drill: false,
      })
      const view = render(homeElement())

      expect(screen.getByRole('status')).toBeInTheDocument()

      authState = { user: user(1, true), isLoading: false }
      view.rerender(homeElement())

      expect(await screen.findByTestId('marketing')).toBeInTheDocument()
      expect(getStatsActivityMock).toHaveBeenCalledTimes(1)
    },
  )

  it('does not assume a first visit when browser storage is unavailable', () => {
    localStorage.clear()
    vi.spyOn(window.localStorage, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    authState = { user: null, isLoading: true }

    render(homeElement())

    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(marketingRender).not.toHaveBeenCalled()
  })

  it('shows root pending during auth loading without requesting activity', () => {
    authState = { user: user(1, true), isLoading: true }

    render(homeElement())

    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(getStatsActivityMock).not.toHaveBeenCalled()
    expect(marketingRender).not.toHaveBeenCalled()
    expect(dashboardRender).not.toHaveBeenCalled()
  })

  it('shows marketing for settled no-user auth without requesting activity', () => {
    render(homeElement())

    expect(screen.getByTestId('marketing')).toBeInTheDocument()
    expect(getStatsActivityMock).not.toHaveBeenCalled()
  })

  it.each([
    { isAnonymous: true, hasActivity: true, expected: 'dashboard' },
    { isAnonymous: true, hasActivity: false, expected: 'marketing' },
    { isAnonymous: false, hasActivity: true, expected: 'dashboard' },
    { isAnonymous: false, hasActivity: false, expected: 'marketing' },
  ])(
    'routes anonymous=$isAnonymous activity=$hasActivity to $expected',
    async ({ isAnonymous, hasActivity, expected }) => {
      authState = { user: user(1, isAnonymous), isLoading: false }
      getStatsActivityMock.mockResolvedValueOnce({
        has_game_or_drill: hasActivity,
      })

      render(homeElement())

      expect(await screen.findByTestId(expected)).toBeInTheDocument()
    },
  )

  it('does not reuse a non-cached decision after an account flips away and back', async () => {
    const secondUser = deferred<{ has_game_or_drill: boolean }>()
    const firstUserAgain = deferred<{ has_game_or_drill: boolean }>()
    getStatsActivityMock
      .mockResolvedValueOnce({ has_game_or_drill: false })
      .mockReturnValueOnce(secondUser.promise)
      .mockReturnValueOnce(firstUserAgain.promise)
    authState = { user: user(1, false), isLoading: false }
    const view = render(homeElement())
    expect(await screen.findByTestId('marketing')).toBeInTheDocument()

    authState = { user: user(2, false), isLoading: false }
    view.rerender(homeElement())
    expect(screen.getByRole('status')).toBeInTheDocument()

    marketingRender.mockClear()
    dashboardRender.mockClear()
    authState = { user: user(1, false), isLoading: false }
    view.rerender(homeElement())

    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(marketingRender).not.toHaveBeenCalled()
    expect(dashboardRender).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(getStatsActivityMock).toHaveBeenCalledTimes(3)
    })

    await act(async () => {
      firstUserAgain.resolve({ has_game_or_drill: true })
    })
    expect(screen.getByTestId('dashboard')).toBeInTheDocument()

    await act(async () => {
      secondUser.resolve({ has_game_or_drill: false })
    })
    expect(screen.getByTestId('dashboard')).toBeInTheDocument()
  })

  it.each([
    {
      label: '401',
      error: new ApiError('unauthorized', { status: 401 }),
      expected: 'marketing',
    },
    {
      label: '429',
      error: new ApiError('limited', { status: 429 }),
      expected: 'marketing',
    },
    {
      label: '503',
      error: new ApiError('unavailable', { status: 503 }),
      expected: 'dashboard',
    },
    { label: 'network', error: new TypeError('offline'), expected: 'dashboard' },
    {
      label: 'timeout',
      error: new DOMException('deadline', 'TimeoutError'),
      expected: 'dashboard',
    },
  ])('routes a $label failure to $expected', async ({ error, expected }) => {
    authState = { user: user(1, false), isLoading: false }
    getStatsActivityMock.mockRejectedValueOnce(error)

    render(homeElement())

    expect(await screen.findByTestId(expected)).toBeInTheDocument()
  })

  it('prevents a late first user response from choosing the second user page', async () => {
    const first = deferred<{ has_game_or_drill: boolean }>()
    const second = deferred<{ has_game_or_drill: boolean }>()
    getStatsActivityMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    authState = { user: user(1, true), isLoading: false }
    const view = render(homeElement())

    marketingRender.mockClear()
    dashboardRender.mockClear()
    authState = { user: user(2, false), isLoading: false }
    view.rerender(homeElement())

    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(marketingRender).not.toHaveBeenCalled()
    expect(dashboardRender).not.toHaveBeenCalled()

    await act(async () => {
      first.resolve({ has_game_or_drill: true })
    })
    expect(screen.getByRole('status')).toBeInTheDocument()

    await act(async () => {
      second.resolve({ has_game_or_drill: false })
    })
    expect(screen.getByTestId('marketing')).toBeInTheDocument()
    expect(dashboardRender).not.toHaveBeenCalled()
  })

  it.each([
    { oldActivity: false, newActivity: true, oldBranch: 'marketing', newBranch: 'dashboard' },
    { oldActivity: true, newActivity: false, oldBranch: 'dashboard', newBranch: 'marketing' },
  ])(
    'synchronously hides a settled $oldBranch decision when the account changes',
    async ({ oldActivity, newActivity, oldBranch, newBranch }) => {
      getStatsActivityMock
        .mockResolvedValueOnce({ has_game_or_drill: oldActivity })
        .mockResolvedValueOnce({ has_game_or_drill: newActivity })
      authState = { user: user(1, false), isLoading: false }
      const view = render(homeElement())
      expect(await screen.findByTestId(oldBranch)).toBeInTheDocument()

      marketingRender.mockClear()
      dashboardRender.mockClear()
      authState = { user: user(2, false), isLoading: false }
      view.rerender(homeElement())

      expect(screen.getByRole('status')).toBeInTheDocument()
      expect(marketingRender).not.toHaveBeenCalled()
      expect(dashboardRender).not.toHaveBeenCalled()
      expect(await screen.findByTestId(newBranch)).toBeInTheDocument()
    },
  )

  it('starts a new same-ID lifecycle on claim while sharing an in-flight request', async () => {
    const activity = deferred<{ has_game_or_drill: boolean }>()
    getStatsActivityMock.mockReturnValueOnce(activity.promise)
    authState = { user: user(1, true), isLoading: false }
    const view = render(homeElement())
    expect(getStatsActivityMock).toHaveBeenCalledTimes(1)

    authState = { user: user(1, false), isLoading: false }
    view.rerender(homeElement())
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(getStatsActivityMock).toHaveBeenCalledTimes(1)

    await act(async () => {
      activity.resolve({ has_game_or_drill: false })
    })
    expect(screen.getByTestId('marketing')).toBeInTheDocument()
  })

  it.each([
    {
      label: 'successful false',
      arrangeFirst: () =>
        getStatsActivityMock.mockResolvedValueOnce({
          has_game_or_drill: false,
        }),
      oldBranch: 'marketing',
      secondActivity: true,
      newBranch: 'dashboard',
    },
    {
      label: 'timeout fallback',
      arrangeFirst: () =>
        getStatsActivityMock.mockRejectedValueOnce(
          new DOMException('deadline', 'TimeoutError'),
        ),
      oldBranch: 'dashboard',
      secondActivity: false,
      newBranch: 'marketing',
    },
  ])(
    'rechecks a settled non-cached $label after a same-ID claim',
    async ({ arrangeFirst, oldBranch, secondActivity, newBranch }) => {
      arrangeFirst()
      getStatsActivityMock.mockResolvedValueOnce({
        has_game_or_drill: secondActivity,
      })
      authState = { user: user(1, true), isLoading: false }
      const view = render(homeElement())
      expect(await screen.findByTestId(oldBranch)).toBeInTheDocument()

      marketingRender.mockClear()
      dashboardRender.mockClear()
      authState = { user: user(1, false), isLoading: false }
      view.rerender(homeElement())

      expect(screen.getByRole('status')).toBeInTheDocument()
      expect(marketingRender).not.toHaveBeenCalled()
      expect(dashboardRender).not.toHaveBeenCalled()
      expect(await screen.findByTestId(newBranch)).toBeInTheDocument()
      expect(getStatsActivityMock).toHaveBeenCalledTimes(2)
    },
  )

  it('keeps a positive same-ID decision through claim without a new request', async () => {
    getStatsActivityMock.mockResolvedValueOnce({ has_game_or_drill: true })
    authState = { user: user(1, true), isLoading: false }
    const view = render(homeElement())
    expect(await screen.findByTestId('dashboard')).toBeInTheDocument()

    authState = { user: user(1, false), isLoading: false }
    view.rerender(homeElement())

    expect(screen.getByTestId('dashboard')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(getStatsActivityMock).toHaveBeenCalledTimes(1)
  })

  it('retries a timeout on remount and can then select marketing', async () => {
    const retry = deferred<{ has_game_or_drill: boolean }>()
    getStatsActivityMock
      .mockRejectedValueOnce(new DOMException('deadline', 'TimeoutError'))
      .mockReturnValueOnce(retry.promise)
    authState = { user: user(1, false), isLoading: false }
    const first = render(homeElement())
    expect(await screen.findByTestId('dashboard')).toBeInTheDocument()

    first.unmount()
    render(homeElement())
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(getStatsActivityMock).toHaveBeenCalledTimes(2)

    await act(async () => {
      retry.resolve({ has_game_or_drill: false })
    })
    expect(screen.getByTestId('marketing')).toBeInTheDocument()
  })

  it('reuses a cached positive on remount without a pending flash', async () => {
    getStatsActivityMock.mockResolvedValueOnce({ has_game_or_drill: true })
    authState = { user: user(1, false), isLoading: false }
    const first = render(homeElement())
    expect(await screen.findByTestId('dashboard')).toBeInTheDocument()
    first.unmount()

    render(homeElement())

    expect(screen.getByTestId('dashboard')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(getStatsActivityMock).toHaveBeenCalledTimes(1)
  })

  it.each(['marketing', 'dashboard'] as const)(
    'keeps root pending while the selected lazy $branch slot suspends',
    async (branch) => {
      const module = deferred<{ default: ComponentType }>()
      const LazyMarker = lazy(() => module.promise)
      if (branch === 'marketing') {
        authState = { user: null, isLoading: false }
      } else {
        authState = { user: user(1, false), isLoading: false }
        getStatsActivityMock.mockResolvedValueOnce({ has_game_or_drill: true })
      }

      render(
        <HomeRoute
          marketingElement={
            branch === 'marketing' ? <LazyMarker /> : <div>marketing</div>
          }
          dashboardElement={
            branch === 'dashboard' ? <LazyMarker /> : <div>dashboard</div>
          }
        />,
      )

      if (branch === 'dashboard') {
        await waitFor(() => {
          expect(hasCachedHomeActivity(1)).toBe(true)
        })
      }
      expect(screen.getByRole('status')).toBeInTheDocument()

      await act(async () => {
        module.resolve({ default: () => <div>{branch}-lazy</div> })
      })
      expect(screen.getByText(`${branch}-lazy`)).toBeInTheDocument()
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    },
  )
})
