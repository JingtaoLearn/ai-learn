import { useIsAuthenticated, useMsal } from '@azure/msal-react'
import { loginRequest } from './msalConfig'

interface Props {
  children: React.ReactNode
}

export function AuthGuard({ children }: Props) {
  const { instance } = useMsal()
  const isAuthenticated = useIsAuthenticated()
  const clientId = import.meta.env.VITE_AZURE_CLIENT_ID

  // Skip auth if Azure client ID is not configured
  if (!clientId) {
    return <>{children}</>
  }

  if (!isAuthenticated) {
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
          <button
            onClick={() => instance.loginPopup(loginRequest).catch(console.error)}
            className="px-6 py-3 bg-blue-600 hover:bg-blue-500 text-white rounded-lg font-medium transition-colors"
          >
            Sign in with Microsoft
          </button>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
