import { useState, useCallback } from 'react'
import { ArrowLeft, XCircle, RefreshCw } from 'lucide-react'
import { useNavigate, useParams } from 'react-router-dom'
import type { Task, Step } from '../types'
import { useApi, apiPost } from '../hooks/useApi'
import { StatusBadge } from './StatusBadge'
import { PipelineView } from './PipelineView'
import { StepDetail } from './StepDetail'

interface TaskWithSteps extends Task {
  steps: Step[]
}

export function TaskDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [selectedStep, setSelectedStep] = useState<Step | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const { data: task, loading, error, refetch } = useApi<TaskWithSteps>(
    `/api/tasks/${id}`,
    { interval: 5000 }
  )

  const handleApprove = useCallback(async (step: Step) => {
    if (!task) return
    setActionError(null)
    try {
      await apiPost(`/api/tasks/${task.id}/steps/${step.id}/approve`)
      await refetch()
      setSelectedStep(null)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Failed to approve')
    }
  }, [task, refetch])

  const handleResume = useCallback(async (step: Step) => {
    if (!task) return
    setActionError(null)
    try {
      await apiPost(`/api/tasks/${task.id}/steps/${step.id}/resume`)
      await refetch()
      setSelectedStep(null)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Failed to resume')
    }
  }, [task, refetch])

  const handleCancel = useCallback(async () => {
    if (!task) return
    if (!confirm(`Cancel task "${task.name}"?`)) return
    setActionError(null)
    try {
      await apiPost(`/api/tasks/${task.id}/cancel`)
      await refetch()
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Failed to cancel')
    }
  }, [task, refetch])

  // Keep selectedStep in sync with fresh data
  const freshSelectedStep = task?.steps?.find(s => s.id === selectedStep?.id) ?? null

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <RefreshCw className="w-6 h-6 text-gray-500 animate-spin" />
      </div>
    )
  }

  if (error || !task) {
    return (
      <div className="text-center py-24">
        <p className="text-red-400">{error ?? 'Task not found'}</p>
        <button onClick={() => navigate('/')} className="mt-4 text-blue-400 hover:underline text-sm">
          Back to tasks
        </button>
      </div>
    )
  }

  const completedSteps = task.steps.filter(s => s.status === 'completed').length

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <button
          onClick={() => navigate('/')}
          className="p-1.5 rounded-lg text-gray-500 hover:text-white hover:bg-white/10 transition-colors"
        >
          <ArrowLeft className="w-5 h-5" />
        </button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-white font-bold text-xl">{task.name}</h2>
            <StatusBadge status={task.status} />
          </div>
          {task.description && (
            <p className="text-gray-500 text-sm mt-0.5">{task.description}</p>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-gray-500 text-sm">{completedSteps}/{task.steps.length} steps</span>
          {(task.status === 'active' || task.status === 'pending') && (
            <button
              onClick={handleCancel}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-red-600/20 hover:bg-red-600/30 text-red-400 border border-red-500/30 rounded-lg text-sm transition-colors"
            >
              <XCircle className="w-4 h-4" />
              Cancel
            </button>
          )}
        </div>
      </div>

      {actionError && (
        <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-3 text-red-400 text-sm">
          {actionError}
        </div>
      )}

      <div className="bg-white/5 border border-white/10 rounded-xl p-4">
        <p className="text-gray-500 text-xs uppercase tracking-wide mb-3">Pipeline</p>
        <PipelineView
          steps={task.steps}
          selectedStepId={freshSelectedStep?.id ?? null}
          onSelectStep={setSelectedStep}
        />
      </div>

      {freshSelectedStep && (
        <StepDetail
          step={freshSelectedStep}
          taskId={task.id}
          onApprove={() => handleApprove(freshSelectedStep)}
          onResume={() => handleResume(freshSelectedStep)}
        />
      )}

      {!freshSelectedStep && (
        <div className="text-center py-8 text-gray-600 text-sm">
          Click a step in the pipeline to see details
        </div>
      )}
    </div>
  )
}
