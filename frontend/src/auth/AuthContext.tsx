import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode
} from 'react'
import { api, ApiError, setAuthToken, setUnauthorizedHandler } from '../api/client'
import type { LoginResponse, User } from '../api/types'

const TOKEN_KEY = 'igp_token'

export interface AuthValue {
  token: string | null
  user: User | null
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthValue | null>(null)

function readStoredToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => {
    const stored = readStoredToken()
    // Keep the api client in sync before the first render fires any request.
    setAuthToken(stored)
    return stored
  })
  const [user, setUser] = useState<User | null>(null)

  const logout = useCallback(() => {
    setAuthToken(null)
    try {
      window.localStorage.removeItem(TOKEN_KEY)
    } catch {
      // ignore
    }
    setToken(null)
    setUser(null)
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(logout)
    return () => setUnauthorizedHandler(null)
  }, [logout])

  useEffect(() => {
    setAuthToken(token)
  }, [token])

  // Validate a restored token and load the user profile.
  useEffect(() => {
    if (!token || user) return
    const ctrl = new AbortController()
    api<User>('/auth/me', { signal: ctrl.signal })
      .then((me) => {
        if (!ctrl.signal.aborted) setUser(me)
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) logout()
      })
    return () => ctrl.abort()
  }, [token, user, logout])

  const login = useCallback(async (email: string, password: string) => {
    const res = await api<LoginResponse>('/auth/login', {
      method: 'POST',
      body: { email, password }
    })
    setAuthToken(res.token)
    try {
      window.localStorage.setItem(TOKEN_KEY, res.token)
    } catch {
      // storage unavailable — token stays in memory only
    }
    setToken(res.token)
    setUser(res.user)
  }, [])

  const register = useCallback(
    async (email: string, password: string) => {
      await api('/auth/register', { method: 'POST', body: { email, password } })
      await login(email, password)
    },
    [login]
  )

  return (
    <AuthContext.Provider value={{ token, user, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}
