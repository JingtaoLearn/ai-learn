import type { TaskStatus, StepStatus } from '../types'

type Status = TaskStatus | StepStatus

const STATUS_CONFIG: Record<string, { label: string; className: string; emoji: string }> = {
  pending:    { label: 'Pending',    className: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/40',  emoji: '⏸️' },
  active:     { label: 'Active',     className: 'bg-blue-500/20 text-blue-300 border-blue-500/40',        emoji: '⏳' },
  executing:  { label: 'Executing',  className: 'bg-blue-600/20 text-blue-200 border-blue-600/40',        emoji: '⚙️' },
  validating: { label: 'Validating', className: 'bg-purple-500/20 text-purple-300 border-purple-500/40', emoji: '🔍' },
  completed:  { label: 'Completed',  className: 'bg-green-500/20 text-green-300 border-green-500/40',     emoji: '✅' },
  failed:     { label: 'Failed',     className: 'bg-red-500/20 text-red-300 border-red-500/40',           emoji: '❌' },
  cancelled:  { label: 'Cancelled',  className: 'bg-gray-500/20 text-gray-400 border-gray-500/40',        emoji: '🚫' },
  blocked:    { label: 'Blocked',    className: 'bg-orange-500/20 text-orange-300 border-orange-500/40', emoji: '🚫' },
  retrying:   { label: 'Retrying',   className: 'bg-yellow-600/20 text-yellow-200 border-yellow-600/40', emoji: '🔄' },
}

interface Props {
  status: Status
  size?: 'sm' | 'md'
}

export function StatusBadge({ status, size = 'md' }: Props) {
  const config = STATUS_CONFIG[status] ?? { label: status, className: 'bg-gray-500/20 text-gray-400 border-gray-500/40', emoji: '?' }
  const sizeClass = size === 'sm' ? 'text-xs px-1.5 py-0.5' : 'text-sm px-2 py-1'
  return (
    <span className={`inline-flex items-center gap-1 rounded border font-medium ${sizeClass} ${config.className}`}>
      <span>{config.emoji}</span>
      <span>{config.label}</span>
    </span>
  )
}

export function getStatusEmoji(status: Status): string {
  return STATUS_CONFIG[status]?.emoji ?? '?'
}
