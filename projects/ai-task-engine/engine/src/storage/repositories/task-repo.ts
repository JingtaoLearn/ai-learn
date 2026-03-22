import { getDb } from '../db';
import { v4 as uuidv4 } from 'uuid';

export type TaskStatus = 'pending' | 'active' | 'completed' | 'cancelled' | 'failed';

export interface TaskRecord {
  id: string;
  workflow_id: string;
  name: string;
  description: string | null;
  status: TaskStatus;
  discord_category_id: string | null;
  discord_overview_channel_id: string | null;
  current_step_index: number;
  context_json: string | null;
  created_at: string;
  updated_at: string;
}

export function createTask(workflowId: string, name: string, description?: string): TaskRecord {
  const db = getDb();
  const id = uuidv4();
  db.prepare(`
    INSERT INTO tasks (id, workflow_id, name, description, status)
    VALUES (?, ?, ?, ?, 'pending')
  `).run(id, workflowId, name, description ?? null);
  return db.prepare('SELECT * FROM tasks WHERE id = ?').get(id) as TaskRecord;
}

export function getTask(id: string): TaskRecord | null {
  return (getDb().prepare('SELECT * FROM tasks WHERE id = ?').get(id) as TaskRecord | undefined) ?? null;
}

export function listTasks(status?: TaskStatus): TaskRecord[] {
  if (status) {
    return getDb().prepare('SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC').all(status) as TaskRecord[];
  }
  return getDb().prepare('SELECT * FROM tasks ORDER BY created_at DESC').all() as TaskRecord[];
}

export function updateTask(id: string, updates: Partial<Omit<TaskRecord, 'id' | 'created_at'>>): void {
  const db = getDb();
  const fields = Object.keys(updates).filter(k => k !== 'updated_at');
  if (fields.length === 0) return;
  const setClause = [...fields.map(f => `${f} = ?`), "updated_at = datetime('now')"].join(', ');
  const values = fields.map(f => (updates as Record<string, unknown>)[f]);
  db.prepare(`UPDATE tasks SET ${setClause} WHERE id = ?`).run(...values, id);
}
