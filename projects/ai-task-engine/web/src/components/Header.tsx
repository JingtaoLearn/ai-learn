import { RefreshCw, Zap, LogOut } from 'lucide-react'
import { useAuth } from '../auth/AuthGuard'

interface Props {
  healthy: boolean | null
  autoRefresh: boolean
  onToggleAutoRefresh: () => void
}

export function Header({ healthy, autoRefresh, onToggleAutoRefresh }: Props) {
  const { user, logout } = useAuth()

  return (
    <header className="border-b border-white/10 bg-[#0f0f1a]/80 backdrop-blur sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center">
            <Zap className="w-4 h-4 text-white" />
          </div>
          <div>
            <h1 className="text-white font-bold text-lg leading-none">AI Task Engine</h1>
            <p className="text-gray-500 text-xs mt-0.5">Task-Driven Assistant System</p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5 text-sm">
            <div
              className={`w-2 h-2 rounded-full ${
                healthy === null
                  ? 'bg-gray-500'
                  : healthy
                  ? 'bg-green-400 shadow-[0_0_6px_#4ade80]'
                  : 'bg-red-400 shadow-[0_0_6px_#f87171]'
              }`}
            />
            <span className="text-gray-400 text-xs">
              {healthy === null ? 'Checking...' : healthy ? 'Online' : 'Offline'}
            </span>
          </div>

          <button
            onClick={onToggleAutoRefresh}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm transition-colors ${
              autoRefresh
                ? 'bg-blue-600/30 text-blue-300 border border-blue-500/40'
                : 'bg-white/5 text-gray-400 border border-white/10 hover:bg-white/10'
            }`}
          >
            <RefreshCw className={`w-3.5 h-3.5 ${autoRefresh ? 'animate-spin' : ''}`} />
            Auto-refresh
          </button>

          {user && (
            <div className="flex items-center gap-2 pl-3 border-l border-white/10">
              <span className="text-gray-400 text-xs">{user.name || user.email}</span>
              <button
                onClick={logout}
                className="p-1.5 rounded-lg bg-white/5 text-gray-400 hover:bg-white/10 border border-white/10 transition-colors"
                title="Sign out"
              >
                <LogOut className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}
