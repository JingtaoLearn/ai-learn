export type TaskStatus = 'pending' | 'active' | 'completed' | 'cancelled' | 'failed'
export type StepStatus = 'pending' | 'active' | 'executing' | 'validating' | 'completed' | 'failed' | 'blocked' | 'retrying'
export type AcceptanceType = 'human_confirm' | 'ai_self_check' | 'automated'

export interface Workflow {
  id: string
  name: string
  description: string | null
  created_at: string
  updated_at: string
}

export interface Task {
  id: string
  workflow_id: string
  name: string
  description: string | null
  status: TaskStatus
  current_step_index: number
  created_at: string
  updated_at: string
  steps?: Step[]
}

export interface Step {
  id: string
  task_id: string
  step_index: number
  name: string
  goal: string
  background: string | null
  rules_json: string | null
  acceptance_type: AcceptanceType
  acceptance_criteria: string | null
  acceptance_command: string | null
  status: StepStatus
  timeout_seconds: number | null
  max_retries: number
  retry_count: number
  output_json: string | null
  error_message: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string
  updated_at: string
}

export interface StepOutput {
  summary: string
  artifacts: Array<{ type: 'file' | 'url'; path?: string; value?: string }>
  metadata: Record<string, unknown>
  completedAt: string
}

export interface StepLog {
  id: number
  step_id: string
  event_type: string
  message: string
  metadata_json: string | null
  created_at: string
}
