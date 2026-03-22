import { Plus, Search, Filter } from 'lucide-react'
import { useState } from 'react'
import type { Task } from '../types'
import { TaskCard } from './TaskCard'

interface Props {
  tasks: Task[]
  onCreateTask: () => void
}

const STATUS_FILTERS = ['all', 'active', 'pending', 'completed', 'failed', 'cancelled'] as const

export function TaskList({ tasks, onCreateTask }: Props) {
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('all')

  const filtered = tasks.filter(t => {
    const matchesSearch =
      t.name.toLowerCase().includes(search.toLowerCase()) ||
      (t.description ?? '').toLowerCase().includes(search.toLowerCase())
    const matchesStatus = statusFilter === 'all' || t.status === statusFilter
    return matchesSearch && matchesStatus
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-white font-semibold text-lg">Tasks</h2>
          <p className="text-gray-500 text-sm">{tasks.length} total</p>
        </div>
        <button
          onClick={onCreateTask}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-sm font-medium transition-colors"
        >
          <Plus className="w-4 h-4" />
          New Task
        </button>
      </div>

      <div className="flex gap-2 mb-4">
        <div className="flex-1 relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search tasks..."
            className="w-full bg-white/5 border border-white/10 rounded-lg pl-9 pr-3 py-2 text-white placeholder-gray-600 text-sm focus:outline-none focus:border-blue-500/50"
          />
        </div>
        <div className="flex items-center gap-1 bg-white/5 border border-white/10 rounded-lg p-1">
          <Filter className="w-3.5 h-3.5 text-gray-500 mx-1.5" />
          {STATUS_FILTERS.map(s => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={`px-2.5 py-1 rounded text-xs font-medium capitalize transition-colors ${
                statusFilter === s
                  ? 'bg-blue-600 text-white'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="text-center py-16 text-gray-600">
          {tasks.length === 0 ? (
            <>
              <p className="text-lg mb-2">No tasks yet</p>
              <p className="text-sm">Create your first task to get started</p>
            </>
          ) : (
            <p>No tasks match your filters</p>
          )}
        </div>
      ) : (
        <div className="space-y-2">
          {filtered.map(task => (
            <TaskCard key={task.id} task={task} />
          ))}
        </div>
      )}
    </div>
  )
}
