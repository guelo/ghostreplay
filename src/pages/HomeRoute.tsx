import { Suspense, useEffect, useState, type ReactNode } from 'react'
import { hasNoStoredAuthIdentity } from '../contexts/authStorage'
import { useAuth } from '../contexts/useAuth'
import {
  hasCachedHomeActivity,
  resolveHomeActivity,
} from '../services/homeActivity'
import { ApiError } from '../utils/api'
import './RootRoute.css'

type HomeBranch = 'marketing' | 'dashboard'

type HomeRouteProps = {
  marketingElement: ReactNode
  dashboardElement: ReactNode
}

const RootPending = () => (
  <div className="home-route-pending" role="status">
    Loading…
  </div>
)

const branchForError = (error: unknown): HomeBranch =>
  error instanceof ApiError && error.status >= 400 && error.status < 500
    ? 'marketing'
    : 'dashboard'

const RootSlot = ({ element }: { element: ReactNode }) => (
  <Suspense fallback={<RootPending />}>{element}</Suspense>
)

type AuthenticatedHomeRouteProps = HomeRouteProps & {
  userId: number
}

function AuthenticatedHomeRoute({
  userId,
  marketingElement,
  dashboardElement,
}: AuthenticatedHomeRouteProps) {
  const [decision, setDecision] = useState<HomeBranch | null>(null)
  const hasPositive = hasCachedHomeActivity(userId)

  useEffect(() => {
    if (hasPositive) {
      return
    }

    let cancelled = false

    resolveHomeActivity(userId)
      .then((hasGameOrDrill) => {
        if (!cancelled) {
          setDecision(hasGameOrDrill ? 'dashboard' : 'marketing')
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setDecision(branchForError(error))
        }
      })

    return () => {
      cancelled = true
    }
  }, [hasPositive, userId])

  const branch = hasPositive ? 'dashboard' : decision

  if (branch === null) {
    return <RootPending />
  }

  return (
    <RootSlot
      element={branch === 'dashboard' ? dashboardElement : marketingElement}
    />
  )
}

function HomeRoute({ marketingElement, dashboardElement }: HomeRouteProps) {
  const { user, isLoading } = useAuth()
  const [isFirstVisit] = useState(
    () => isLoading && hasNoStoredAuthIdentity(),
  )

  if (isFirstVisit) {
    return <RootSlot element={marketingElement} />
  }

  if (isLoading) {
    return <RootPending />
  }

  if (!user) {
    return <RootSlot element={marketingElement} />
  }

  return (
    <AuthenticatedHomeRoute
      key={`${user.id}:${user.isAnonymous ? 'anonymous' : 'claimed'}`}
      userId={user.id}
      marketingElement={marketingElement}
      dashboardElement={dashboardElement}
    />
  )
}

export default HomeRoute
