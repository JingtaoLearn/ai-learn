import { useState } from 'react'
import { X, Loader2 } from 'lucide-react'
import type { Workflow } from '../types'
import { apiPost } from '../hooks/useApi'

interface Props {
  workflows: Workflow[]
  onClose: () => void
  onCreated: () => void
}

export function CreateTaskForm({ workflows, onClose, onCreated }: Props) {
  const [workflowId, setWorkflowId] = useState(workflows[0]?.id ?? '')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim() || !workflowId) return
    setLoading(true)
    setError(null)
    try {
      await apiPost('/api/tasks', { workflowId, name: name.trim(), description: description.trim() || undefined })
      onCreated()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create task')
      setLoading(false)
    }
  }

  const selectedWorkflow = workflows.find(w => w.id === workflowId)

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-[#1a1a2e] border border-white/15 rounded-2xl w-full max-w-lg shadow-2xl">
        <div className="flex items-center justify-between p-5 border-b border-white/10">
          <h2 className="text-white font-semibold text-lg">Create New Task</h2>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-gray-500 hover:text-white hover:bg-white/10 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          <div>
            <label className="block text-gray-400 text-sm mb-1.5">Workflow</label>
            <select
              value={workflowId}
              onChange={e => setWorkflowId(e.target.value)}
              className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-blue-500/50 appearance-none"
            >
              {workflows.map(w => (
                <option key={w.id} value={w.id} className="bg-[#1a1a2e]">
                  {w.name}
                </option>
              ))}
            </select>
            {selectedWorkflow?.description && (
              <p className="text-gray-600 text-xs mt-1.5">{selectedWorkflow.description}</p>
            )}
          </div>

          <div>
            <label className="block text-gray-400 text-sm mb-1.5">Task Name</label>
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="e.g. Fix login bug in auth service"
              required
              className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-white placeholder-gray-600 text-sm focus:outline-none focus:border-blue-500/50"
            />
          </div>

          <div>
            <label className="block text-gray-400 text-sm mb-1.5">
              Description <span className="text-gray-600">(optional)</span>
            </label>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="Additional context for this task..."
              rows={3}
              className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-white placeholder-gray-600 text-sm focus:outline-none focus:border-blue-500/50 resize-none"
            />
          </div>

          {error && (
            <p className="text-red-400 text-sm bg-red-500/10 border border-red-500/20 rounded-lg p-3">
              {error}
            </p>
          )}

          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2.5 border border-white/10 text-gray-400 rounded-lg text-sm hover:bg-white/5 transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading || !name.trim()}
              className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg text-sm font-medium transition-colors"
            >
              {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
              Create Task
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
