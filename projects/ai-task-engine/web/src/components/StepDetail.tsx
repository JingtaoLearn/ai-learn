import { useState } from 'react'
import { CheckCircle, RotateCcw, AlertCircle, FileText, Clock, RefreshCw } from 'lucide-react'
import type { Step, StepOutput, StepLog } from '../types'
import { StatusBadge } from './StatusBadge'
import { useApi } from '../hooks/useApi'

interface Props {
  step: Step
  taskId: string
  onApprove: () => void
  onResume: () => void
}

function parseOutput(json: string | null): StepOutput | null {
  if (!json) return null
  try { return JSON.parse(json) } catch { return null }
}

function parseRules(json: string | null): string[] {
  if (!json) return []
  try { return JSON.parse(json) } catch { return [] }
}

function formatDuration(start: string | null, end: string | null): string {
  if (!start) return '-'
  const startMs = new Date(start).getTime()
  const endMs = end ? new Date(end).getTime() : Date.now()
  const diff = Math.floor((endMs - startMs) / 1000)
  if (diff < 60) return `${diff}s`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ${diff % 60}s`
  return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`
}

export function StepDetail({ step, taskId, onApprove, onResume }: Props) {
  const [activeTab, setActiveTab] = useState<'info' | 'output' | 'logs'>('info')
  const output = parseOutput(step.output_json)
  const rules = parseRules(step.rules_json)

  const { data: logs } = useApi<StepLog[]>(
    `/api/tasks/${taskId}/steps/${step.id}/logs`,
    { enabled: activeTab === 'logs' }
  )

  const canApprove = step.status === 'active' && step.acceptance_type === 'human_confirm'
  const canResume = step.status === 'failed' || step.status === 'blocked'

  return (
    <div className="bg-white/5 border border-white/10 rounded-xl overflow-hidden">
      <div className="p-4 border-b border-white/10">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <div className="flex items-center gap-2">
              <span className="text-gray-500 text-sm">#{step.step_index + 1}</span>
              <h3 className="text-white font-semibold">{step.name}</h3>
            </div>
            <div className="flex items-center gap-3 mt-1.5">
              <StatusBadge status={step.status} size="sm" />
              <span className="text-gray-500 text-xs">{step.acceptance_type}</span>
              {step.retry_count > 0 && (
                <span className="text-yellow-500 text-xs flex items-center gap-1">
                  <RefreshCw className="w-3 h-3" />
                  Retry {step.retry_count}/{step.max_retries}
                </span>
              )}
            </div>
          </div>
          <div className="flex gap-2">
            {canApprove && (
              <button
                onClick={onApprove}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 hover:bg-green-500 text-white rounded-lg text-sm font-medium transition-colors"
              >
                <CheckCircle className="w-4 h-4" />
                Approve
              </button>
            )}
            {canResume && (
              <button
                onClick={onResume}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-yellow-600 hover:bg-yellow-500 text-white rounded-lg text-sm font-medium transition-colors"
              >
                <RotateCcw className="w-4 h-4" />
                Resume
              </button>
            )}
          </div>
        </div>

        <div className="flex items-center gap-4 mt-3 text-xs text-gray-500">
          <span className="flex items-center gap-1">
            <Clock className="w-3 h-3" />
            Duration: {formatDuration(step.started_at, step.completed_at)}
          </span>
          {step.timeout_seconds && (
            <span>Timeout: {Math.floor(step.timeout_seconds / 60)}m</span>
          )}
        </div>
      </div>

      <div className="flex border-b border-white/10">
        {(['info', 'output', 'logs'] as const).map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 text-sm font-medium capitalize transition-colors ${
              activeTab === tab
                ? 'text-white border-b-2 border-blue-500'
                : 'text-gray-500 hover:text-gray-300'
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      <div className="p-4">
        {activeTab === 'info' && (
          <div className="space-y-4">
            <div>
              <p className="text-gray-500 text-xs uppercase tracking-wide mb-1.5">Goal</p>
              <p className="text-gray-200 text-sm whitespace-pre-wrap">{step.goal}</p>
            </div>
            {step.background && (
              <div>
                <p className="text-gray-500 text-xs uppercase tracking-wide mb-1.5">Background</p>
                <p className="text-gray-400 text-sm whitespace-pre-wrap">{step.background}</p>
              </div>
            )}
            {rules.length > 0 && (
              <div>
                <p className="text-gray-500 text-xs uppercase tracking-wide mb-1.5">Rules</p>
                <ul className="space-y-1">
                  {rules.map((rule, i) => (
                    <li key={i} className="text-gray-400 text-sm flex gap-2">
                      <span className="text-gray-600 shrink-0">•</span>
                      {rule}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {step.error_message && (
              <div>
                <p className="text-red-400 text-xs uppercase tracking-wide mb-1.5 flex items-center gap-1">
                  <AlertCircle className="w-3 h-3" />
                  Error
                </p>
                <p className="text-red-300 text-sm bg-red-500/10 border border-red-500/20 rounded-lg p-3 font-mono">
                  {step.error_message}
                </p>
              </div>
            )}
          </div>
        )}

        {activeTab === 'output' && (
          <div>
            {output ? (
              <div className="space-y-4">
                <div>
                  <p className="text-gray-500 text-xs uppercase tracking-wide mb-1.5">Summary</p>
                  <p className="text-gray-200 text-sm whitespace-pre-wrap">{output.summary}</p>
                </div>
                {output.artifacts.length > 0 && (
                  <div>
                    <p className="text-gray-500 text-xs uppercase tracking-wide mb-1.5">Artifacts</p>
                    <div className="space-y-1">
                      {output.artifacts.map((a, i) => (
                        <div key={i} className="flex items-center gap-2 text-sm">
                          <FileText className="w-3.5 h-3.5 text-gray-500" />
                          <span className="text-blue-400 font-mono text-xs">{a.path ?? a.value}</span>
                          <span className="text-gray-600 text-xs">{a.type}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {Object.keys(output.metadata).length > 0 && (
                  <div>
                    <p className="text-gray-500 text-xs uppercase tracking-wide mb-1.5">Metadata</p>
                    <pre className="text-gray-400 text-xs bg-black/30 rounded-lg p-3 overflow-auto">
                      {JSON.stringify(output.metadata, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            ) : (
              <p className="text-gray-600 text-sm text-center py-8">No output yet</p>
            )}
          </div>
        )}

        {activeTab === 'logs' && (
          <div>
            {logs && logs.length > 0 ? (
              <div className="space-y-1 max-h-64 overflow-y-auto">
                {logs.map(log => (
                  <div key={log.id} className="flex gap-3 text-xs">
                    <span className="text-gray-600 font-mono shrink-0">
                      {new Date(log.created_at).toLocaleTimeString()}
                    </span>
                    <span className="text-blue-400 shrink-0">{log.event_type}</span>
                    <span className="text-gray-400">{log.message}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-gray-600 text-sm text-center py-8">No logs</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
