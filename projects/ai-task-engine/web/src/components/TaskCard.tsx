import { Clock, ChevronRight } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import type { Task, Step } from '../types'
import { StatusBadge } from './StatusBadge'

function formatRelative(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime()
  const s = Math.floor(diff / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

interface Props {
  task: Task & { steps?: Step[] }
}

export function TaskCard({ task }: Props) {
  const navigate = useNavigate()
  const steps = task.steps ?? []
  const completedSteps = steps.filter(s => s.status === 'completed').length
  const totalSteps = steps.length

  return (
    <div
      onClick={() => navigate(`/tasks/${task.id}`)}
      className="group bg-white/5 border border-white/10 rounded-xl p-4 cursor-pointer hover:bg-white/8 hover:border-white/20 transition-all"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-white font-semibold truncate">{task.name}</h3>
            <StatusBadge status={task.status} size="sm" />
          </div>
          {task.description && (
            <p className="text-gray-500 text-sm mt-1 line-clamp-1">{task.description}</p>
          )}
          <div className="flex items-center gap-4 mt-2">
            <span className="text-gray-500 text-xs font-mono">{task.workflow_id}</span>
            {totalSteps > 0 && (
              <span className="text-gray-400 text-xs">
                {completedSteps}/{totalSteps} steps
              </span>
            )}
            <span className="text-gray-600 text-xs flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {formatRelative(task.created_at)}
            </span>
          </div>
        </div>
        <ChevronRight className="w-4 h-4 text-gray-600 group-hover:text-gray-400 flex-shrink-0 mt-1 transition-colors" />
      </div>

      {totalSteps > 0 && (
        <div className="mt-3 flex gap-1">
          {steps.map(step => (
            <div
              key={step.id}
              title={step.name}
              className={`h-1 flex-1 rounded-full ${
                step.status === 'completed'
                  ? 'bg-green-500'
                  : step.status === 'failed'
                  ? 'bg-red-500'
                  : step.status === 'active' || step.status === 'executing' || step.status === 'validating'
                  ? 'bg-blue-500'
                  : step.status === 'blocked'
                  ? 'bg-orange-500'
                  : 'bg-white/10'
              }`}
            />
          ))}
        </div>
      )}
    </div>
  )
}
