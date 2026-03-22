import { useState, useEffect, createContext, useContext, useCallback } from 'react'

interface User {
  email: string
  name: string
}

interface AuthState {
  authenticated: boolean
  user: User | null
  logout: () => void
}

const AuthContext = createContext<AuthState>({ authenticated: false, user: null, logout: () => {} })

export function useAuth() {
  return useContext(AuthContext)
}

interface Props {
  children: React.ReactNode
}

export function AuthGuard({ children }: Props) {
  const [loading, setLoading] = useState(true)
  const [authenticated, setAuthenticated] = useState(false)
  const [user, setUser] = useState<User | null>(null)
  const [loginUrl, setLoginUrl] = useState<string | null>(null)

  useEffect(() => {
    fetch('/auth/me')
      .then(res => res.json())
      .then(data => {
        if (data.authenticated) {
          setAuthenticated(true)
          setUser(data.user)
        } else {
          return fetch('/auth/config')
            .then(res => res.json())
            .then(config => setLoginUrl(config.loginUrl))
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const logout = useCallback(() => {
    fetch('/auth/logout')
      .then(() => {
        setAuthenticated(false)
        setUser(null)
        window.location.reload()
      })
      .catch(console.error)
  }, [])

  if (loading) {
    return (
      <div className="min-h-screen bg-[#0f0f1a] flex items-center justify-center">
        <div className="text-gray-400">Loading...</div>
      </div>
    )
  }

  if (!authenticated) {
    return (
      <div className="min-h-screen bg-[#0f0f1a] flex items-center justify-center">
        <div className="text-center">
          <div className="w-16 h-16 rounded-2xl bg-blue-600 flex items-center justify-center mx-auto mb-6">
            <svg className="w-8 h-8 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          </div>
          <h1 className="text-white text-2xl font-bold mb-2">AI Task Engine</h1>
          <p className="text-gray-400 mb-8">Sign in with your Microsoft account to continue</p>
          {loginUrl && (
            <a
              href={loginUrl}
              className="inline-block px-6 py-3 bg-blue-600 hover:bg-blue-500 text-white rounded-lg font-medium transition-colors"
            >
              Sign in with Microsoft
            </a>
          )}
        </div>
      </div>
    )
  }

  return (
    <AuthContext.Provider value={{ authenticated, user, logout }}>
      {children}
    </AuthContext.Provider>
  )
}
