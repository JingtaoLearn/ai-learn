import { ArrowRight } from 'lucide-react'
import type { Step } from '../types'
import { StepNode } from './StepNode'

interface Props {
  steps: Step[]
  selectedStepId: string | null
  onSelectStep: (step: Step) => void
}

export function PipelineView({ steps, selectedStepId, onSelectStep }: Props) {
  if (steps.length === 0) {
    return (
      <div className="text-center py-12 text-gray-600">
        No steps in this pipeline
      </div>
    )
  }

  return (
    <div className="overflow-x-auto pb-2">
      <div className="flex items-center gap-0 min-w-max py-4 px-1">
        {steps.map((step, index) => (
          <div key={step.id} className="flex items-center">
            <StepNode
              step={step}
              isSelected={selectedStepId === step.id}
              onClick={() => onSelectStep(step)}
            />
            {index < steps.length - 1 && (
              <div className="flex items-center mx-1">
                <div
                  className={`w-6 h-0.5 ${
                    steps[index + 1].status !== 'pending'
                      ? 'bg-green-500/60'
                      : 'bg-white/15'
                  }`}
                />
                <ArrowRight
                  className={`w-3 h-3 -ml-1 ${
                    steps[index + 1].status !== 'pending'
                      ? 'text-green-500/60'
                      : 'text-white/15'
                  }`}
                />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
