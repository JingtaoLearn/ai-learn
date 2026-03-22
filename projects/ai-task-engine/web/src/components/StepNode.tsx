import type { Step } from '../types'
import { getStatusEmoji } from './StatusBadge'

interface Props {
  step: Step
  isSelected: boolean
  onClick: () => void
}

const STEP_COLORS: Record<string, string> = {
  pending:    'border-white/10 bg-white/5 text-gray-400',
  active:     'border-blue-500/60 bg-blue-500/10 text-blue-300 shadow-[0_0_12px_rgba(59,130,246,0.2)]',
  executing:  'border-blue-600/60 bg-blue-600/10 text-blue-200 shadow-[0_0_12px_rgba(37,99,235,0.2)]',
  validating: 'border-purple-500/60 bg-purple-500/10 text-purple-300',
  completed:  'border-green-500/40 bg-green-500/10 text-green-300',
  failed:     'border-red-500/60 bg-red-500/10 text-red-300',
  cancelled:  'border-gray-500/30 bg-gray-500/5 text-gray-500',
  blocked:    'border-orange-500/60 bg-orange-500/10 text-orange-300',
  retrying:   'border-yellow-500/60 bg-yellow-500/10 text-yellow-300',
}

export function StepNode({ step, isSelected, onClick }: Props) {
  const colorClass = STEP_COLORS[step.status] ?? STEP_COLORS.pending

  return (
    <button
      onClick={onClick}
      className={`relative flex flex-col items-center gap-1 px-3 py-2.5 rounded-xl border transition-all min-w-[90px] max-w-[120px] ${colorClass} ${
        isSelected ? 'ring-2 ring-white/30 ring-offset-1 ring-offset-[#0f0f1a]' : 'hover:brightness-125'
      }`}
    >
      <span className="text-xl leading-none">{getStatusEmoji(step.status)}</span>
      <span className="text-xs font-medium text-center leading-tight line-clamp-2 w-full">{step.name}</span>
      <span className="text-[10px] opacity-60">#{step.step_index + 1}</span>
    </button>
  )
}
