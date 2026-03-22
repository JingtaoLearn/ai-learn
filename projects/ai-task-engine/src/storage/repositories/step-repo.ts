import { getDb } from '../db';
import { v4 as uuidv4 } from 'uuid';

export type StepStatus = 'pending' | 'active' | 'executing' | 'validating' | 'completed' | 'failed' | 'blocked' | 'retrying';
export type AcceptanceType = 'human_confirm' | 'ai_self_check' | 'automated';
export type WakePolicy = 'timeout' | 'dependency' | 'manual';

export interface StepRecord {
  id: string;
  task_id: string;
  step_index: number;
  name: string;
  goal: string;
  background: string | null;
  rules_json: string | null;
  acceptance_type: AcceptanceType;
  acceptance_criteria: string | null;
  acceptance_command: string | null;
  status: StepStatus;
  discord_channel_id: string | null;
  timeout_seconds: number | null;
  wake_policy: WakePolicy | null;
  max_retries: number;
  retry_count: number;
  output_json: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface StepOutput {
  summary: string;
  artifacts: Array<{ type: 'file' | 'url'; path?: string; value?: string }>;
  metadata: Record<string, unknown>;
  completedAt: string;
}

export function createStep(params: {
  taskId: string;
  stepIndex: number;
  name: string;
  goal: string;
  background?: string;
  rules?: string[];
  acceptanceType: AcceptanceType;
  acceptanceCriteria?: string;
  acceptanceCommand?: string;
  timeoutSeconds?: number;
  wakePolicy?: WakePolicy;
  maxRetries?: number;
}): StepRecord {
  const db = getDb();
  const id = uuidv4();
  db.prepare(`
    INSERT INTO steps (
      id, task_id, step_index, name, goal, background, rules_json,
      acceptance_type, acceptance_criteria, acceptance_command,
      timeout_seconds, wake_policy, max_retries
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).run(
    id, params.taskId, params.stepIndex, params.name, params.goal,
    params.background ?? null,
    params.rules ? JSON.stringify(params.rules) : null,
    params.acceptanceType,
    params.acceptanceCriteria ?? null,
    params.acceptanceCommand ?? null,
    params.timeoutSeconds ?? null,
    params.wakePolicy ?? 'dependency',
    params.maxRetries ?? 0
  );
  return db.prepare('SELECT * FROM steps WHERE id = ?').get(id) as StepRecord;
}

export function getStep(id: string): StepRecord | null {
  return (getDb().prepare('SELECT * FROM steps WHERE id = ?').get(id) as StepRecord | undefined) ?? null;
}

export function getStepsByTask(taskId: string): StepRecord[] {
  return getDb().prepare('SELECT * FROM steps WHERE task_id = ? ORDER BY step_index').all(taskId) as StepRecord[];
}

export function updateStep(id: string, updates: Partial<Omit<StepRecord, 'id' | 'created_at'>>): void {
  const db = getDb();
  const fields = Object.keys(updates).filter(k => k !== 'updated_at');
  if (fields.length === 0) return;
  const setClause = [...fields.map(f => `${f} = ?`), "updated_at = datetime('now')"].join(', ');
  const values = fields.map(f => (updates as Record<string, unknown>)[f]);
  db.prepare(`UPDATE steps SET ${setClause} WHERE id = ?`).run(...values, id);
}

export function logStepEvent(stepId: string, eventType: string, message: string, metadata?: Record<string, unknown>): void {
  getDb().prepare(`
    INSERT INTO step_logs (step_id, event_type, message, metadata_json)
    VALUES (?, ?, ?, ?)
  `).run(stepId, eventType, message, metadata ? JSON.stringify(metadata) : null);
}

export function getStepLogs(stepId: string): Array<{
  id: number; step_id: string; event_type: string; message: string | null;
  metadata_json: string | null; created_at: string;
}> {
  return getDb().prepare('SELECT * FROM step_logs WHERE step_id = ? ORDER BY id').all(stepId) as any[];
}

export function getActiveStepsWithTimeout(): StepRecord[] {
  return getDb().prepare(`
    SELECT * FROM steps
    WHERE status IN ('active', 'executing')
    AND timeout_seconds IS NOT NULL
    AND started_at IS NOT NULL
  `).all() as StepRecord[];
}
