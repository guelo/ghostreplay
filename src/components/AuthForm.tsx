import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

interface AuthFormProps {
  mode: 'register' | 'login'
}

export default function AuthForm({ mode }: AuthFormProps) {
  const { login, claimAccount } = useAuth()
  const navigate = useNavigate()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const validate = (): string | null => {
    if (username.length < 3 || username.length > 50) {
      return 'Username must be 3\u201350 characters'
    }
    if (password.length < 6) {
      return 'Password must be at least 6 characters'
    }
    if (mode === 'register' && password !== confirmPassword) {
      return 'Passwords do not match'
    }
    return null
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    const validationError = validate()
    if (validationError) {
      setError(validationError)
      return
    }

    setError(null)
    setSubmitting(true)
    try {
      if (mode === 'register') {
        await claimAccount(username, password)
      } else {
        await login(username, password)
      }
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong')
    } finally {
      setSubmitting(false)
    }
  }

  const isRegister = mode === 'register'
  const title = isRegister ? 'Create your account' : 'Log in'
  const buttonLabel = isRegister ? 'Register' : 'Log in'

  return (
    <main className="app-shell">
      <div className="auth-page">
        <form className="auth-form" onSubmit={handleSubmit}>
          <h1 className="auth-form__title">{title}</h1>

          <label className="auth-form__label">
            Username
            <input
              className="auth-form__input"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
            />
          </label>

          <label className="auth-form__label">
            Password
            <input
              className="auth-form__input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={isRegister ? 'new-password' : 'current-password'}
            />
          </label>

          {isRegister && (
            <label className="auth-form__label">
              Confirm password
              <input
                className="auth-form__input"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                autoComplete="new-password"
              />
            </label>
          )}

          {error && <p className="auth-form__error">{error}</p>}

          <button
            className="chess-button primary auth-form__submit"
            type="submit"
            disabled={submitting}
          >
            {submitting ? 'Please wait\u2026' : buttonLabel}
          </button>

          <p className="auth-form__footer">
            {isRegister ? (
              <>Already have an account? <Link to="/login">Log in</Link></>
            ) : (
              <>New here? <Link to="/register">Register</Link></>
            )}
          </p>
        </form>
      </div>
    </main>
  )
}
