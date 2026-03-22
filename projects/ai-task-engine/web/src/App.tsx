import { useState } from 'react'
import { Routes, Route } from 'react-router-dom'
import { Header } from './components/Header'
import { TaskList } from './components/TaskList'
import { TaskDetail } from './components/TaskDetail'
import { CreateTaskForm } from './components/CreateTaskForm'
import { useApi, useHealth } from './hooks/useApi'
import type { Task, Workflow } from './types'

function HomePage({
  autoRefresh,
  onCreateTask,
  refreshKey,
}: {
  autoRefresh: boolean
  onCreateTask: () => void
  refreshKey: number
}) {
  const { data: tasks } = useApi<Task[]>(`/api/tasks?_r=${refreshKey}`, {
    interval: autoRefresh ? 5000 : undefined,
  })

  return <TaskList tasks={tasks ?? []} onCreateTask={onCreateTask} />
}

export default function App() {
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const healthy = useHealth(autoRefresh)

  const { data: workflows } = useApi<Workflow[]>('/api/workflows')

  return (
    <div className="min-h-screen bg-[#0f0f1a] text-white">
      <Header
        healthy={healthy}
        autoRefresh={autoRefresh}
        onToggleAutoRefresh={() => setAutoRefresh(v => !v)}
      />

      <main className="max-w-7xl mx-auto px-4 py-6">
        <Routes>
          <Route
            path="/"
            element={
              <HomePage
                autoRefresh={autoRefresh}
                onCreateTask={() => setShowCreate(true)}
                refreshKey={refreshKey}
              />
            }
          />
          <Route path="/tasks/:id" element={<TaskDetail />} />
        </Routes>
      </main>

      {showCreate && (
        <CreateTaskForm
          workflows={workflows ?? []}
          onClose={() => setShowCreate(false)}
          onCreated={() => setRefreshKey(k => k + 1)}
        />
      )}
    </div>
  )
}
