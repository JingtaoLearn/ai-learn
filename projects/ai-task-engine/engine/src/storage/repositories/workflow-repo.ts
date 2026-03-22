import { getDb } from '../db';
import { v4 as uuidv4 } from 'uuid';

export interface WorkflowRecord {
  id: string;
  name: string;
  description: string | null;
  yaml_content: string;
  created_at: string;
  updated_at: string;
}

export function upsertWorkflow(name: string, description: string | null, yamlContent: string): WorkflowRecord {
  const db = getDb();
  const existing = db.prepare('SELECT * FROM workflows WHERE name = ?').get(name) as WorkflowRecord | undefined;

  if (existing) {
    db.prepare(`
      UPDATE workflows SET description = ?, yaml_content = ?, updated_at = datetime('now') WHERE id = ?
    `).run(description, yamlContent, existing.id);
    return db.prepare('SELECT * FROM workflows WHERE id = ?').get(existing.id) as WorkflowRecord;
  }

  const id = uuidv4();
  db.prepare(`
    INSERT INTO workflows (id, name, description, yaml_content) VALUES (?, ?, ?, ?)
  `).run(id, name, description, yamlContent);
  return db.prepare('SELECT * FROM workflows WHERE id = ?').get(id) as WorkflowRecord;
}

export function getWorkflowByName(name: string): WorkflowRecord | null {
  return (getDb().prepare('SELECT * FROM workflows WHERE name = ?').get(name) as WorkflowRecord | undefined) ?? null;
}

export function listWorkflows(): WorkflowRecord[] {
  return getDb().prepare('SELECT * FROM workflows ORDER BY name').all() as WorkflowRecord[];
}
