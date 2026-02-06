import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import AuthForm from './AuthForm'

const mockLogin = vi.fn()
const mockClaimAccount = vi.fn()
const mockNavigate = vi.fn()

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    login: mockLogin,
    claimAccount: mockClaimAccount,
    user: { id: 1, username: 'ghost_abc', isAnonymous: true },
    token: 'mock-token',
    isLoading: false,
    error: null,
    logout: vi.fn(),
  }),
}))

function renderForm(mode: 'register' | 'login') {
  return render(
    <MemoryRouter>
      <AuthForm mode={mode} />
    </MemoryRouter>,
  )
}

describe('AuthForm', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('register mode', () => {
    it('renders username, password, and confirm password fields', () => {
      renderForm('register')
      expect(screen.getByLabelText('Username')).toBeInTheDocument()
      expect(screen.getByLabelText('Password')).toBeInTheDocument()
      expect(screen.getByLabelText('Confirm password')).toBeInTheDocument()
    })

    it('renders Register submit button', () => {
      renderForm('register')
      expect(screen.getByRole('button', { name: 'Register' })).toBeInTheDocument()
    })

    it('shows link to login page', () => {
      renderForm('register')
      expect(screen.getByText('Log in')).toHaveAttribute('href', '/login')
    })

    it('validates short username', async () => {
      const user = userEvent.setup()
      renderForm('register')

      await user.type(screen.getByLabelText('Username'), 'ab')
      await user.type(screen.getByLabelText('Password'), 'password123')
      await user.type(screen.getByLabelText('Confirm password'), 'password123')
      await user.click(screen.getByRole('button', { name: 'Register' }))

      expect(screen.getByText(/Username must be 3/)).toBeInTheDocument()
      expect(mockClaimAccount).not.toHaveBeenCalled()
    })

    it('validates short password', async () => {
      const user = userEvent.setup()
      renderForm('register')

      await user.type(screen.getByLabelText('Username'), 'myuser')
      await user.type(screen.getByLabelText('Password'), 'ab123')
      await user.type(screen.getByLabelText('Confirm password'), 'ab123')
      await user.click(screen.getByRole('button', { name: 'Register' }))

      expect(screen.getByText(/Password must be at least 6/)).toBeInTheDocument()
      expect(mockClaimAccount).not.toHaveBeenCalled()
    })

    it('validates password mismatch', async () => {
      const user = userEvent.setup()
      renderForm('register')

      await user.type(screen.getByLabelText('Username'), 'myuser')
      await user.type(screen.getByLabelText('Password'), 'password123')
      await user.type(screen.getByLabelText('Confirm password'), 'differentpass')
      await user.click(screen.getByRole('button', { name: 'Register' }))

      expect(screen.getByText('Passwords do not match')).toBeInTheDocument()
      expect(mockClaimAccount).not.toHaveBeenCalled()
    })

    it('calls claimAccount and navigates on success', async () => {
      const user = userEvent.setup()
      mockClaimAccount.mockResolvedValue(undefined)
      renderForm('register')

      await user.type(screen.getByLabelText('Username'), 'myuser')
      await user.type(screen.getByLabelText('Password'), 'password123')
      await user.type(screen.getByLabelText('Confirm password'), 'password123')
      await user.click(screen.getByRole('button', { name: 'Register' }))

      await waitFor(() => {
        expect(mockClaimAccount).toHaveBeenCalledWith('myuser', 'password123')
        expect(mockNavigate).toHaveBeenCalledWith('/')
      })
    })

    it('displays API error on failure', async () => {
      const user = userEvent.setup()
      mockClaimAccount.mockRejectedValue(new Error('Username already taken'))
      renderForm('register')

      await user.type(screen.getByLabelText('Username'), 'myuser')
      await user.type(screen.getByLabelText('Password'), 'password123')
      await user.type(screen.getByLabelText('Confirm password'), 'password123')
      await user.click(screen.getByRole('button', { name: 'Register' }))

      await waitFor(() => {
        expect(screen.getByText('Username already taken')).toBeInTheDocument()
      })
    })
  })

  describe('login mode', () => {
    it('renders username and password fields but not confirm password', () => {
      renderForm('login')
      expect(screen.getByLabelText('Username')).toBeInTheDocument()
      expect(screen.getByLabelText('Password')).toBeInTheDocument()
      expect(screen.queryByLabelText('Confirm password')).not.toBeInTheDocument()
    })

    it('renders Log in submit button', () => {
      renderForm('login')
      expect(screen.getByRole('button', { name: 'Log in' })).toBeInTheDocument()
    })

    it('shows link to register page', () => {
      renderForm('login')
      expect(screen.getByText('Register')).toHaveAttribute('href', '/register')
    })

    it('calls login and navigates on success', async () => {
      const user = userEvent.setup()
      mockLogin.mockResolvedValue(undefined)
      renderForm('login')

      await user.type(screen.getByLabelText('Username'), 'myuser')
      await user.type(screen.getByLabelText('Password'), 'password123')
      await user.click(screen.getByRole('button', { name: 'Log in' }))

      await waitFor(() => {
        expect(mockLogin).toHaveBeenCalledWith('myuser', 'password123')
        expect(mockNavigate).toHaveBeenCalledWith('/')
      })
    })

    it('displays API error on failure', async () => {
      const user = userEvent.setup()
      mockLogin.mockRejectedValue(new Error('Invalid credentials'))
      renderForm('login')

      await user.type(screen.getByLabelText('Username'), 'myuser')
      await user.type(screen.getByLabelText('Password'), 'password123')
      await user.click(screen.getByRole('button', { name: 'Log in' }))

      await waitFor(() => {
        expect(screen.getByText('Invalid credentials')).toBeInTheDocument()
      })
    })
  })

  describe('submit button state', () => {
    it('disables button and shows loading text while submitting', async () => {
      const user = userEvent.setup()
      let resolveLogin: () => void
      mockLogin.mockReturnValue(
        new Promise<void>((resolve) => { resolveLogin = resolve }),
      )
      renderForm('login')

      await user.type(screen.getByLabelText('Username'), 'myuser')
      await user.type(screen.getByLabelText('Password'), 'password123')
      await user.click(screen.getByRole('button', { name: 'Log in' }))

      expect(screen.getByRole('button', { name: /Please wait/ })).toBeDisabled()

      resolveLogin!()
      await waitFor(() => {
        expect(mockNavigate).toHaveBeenCalledWith('/')
      })
    })
  })
})
