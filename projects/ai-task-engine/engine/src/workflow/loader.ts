import fs from 'fs';
import path from 'path';
import yaml from 'js-yaml';
import { WorkflowSchema, WorkflowDefinition } from './schema';
import { upsertWorkflow, listWorkflows } from '../storage/repositories/workflow-repo';

const DEFAULT_WORKFLOWS_DIR = path.join(process.cwd(), 'workflows');

export function getWorkflowsDir(): string {
  return process.env.WORKFLOWS_DIR || DEFAULT_WORKFLOWS_DIR;
}

export function loadWorkflowFromFile(filePath: string): WorkflowDefinition {
  const content = fs.readFileSync(filePath, 'utf-8');
  const raw = yaml.load(content);
  const result = WorkflowSchema.safeParse(raw);
  if (!result.success) {
    throw new Error(`Invalid workflow file ${filePath}: ${result.error.message}`);
  }
  return result.data;
}

export function syncWorkflowsFromDisk(): void {
  const dir = getWorkflowsDir();
  if (!fs.existsSync(dir)) {
    console.warn(`[workflow-loader] Workflows directory not found: ${dir}`);
    return;
  }

  const files = fs.readdirSync(dir).filter(f => f.endsWith('.yaml') || f.endsWith('.yml'));
  for (const file of files) {
    const filePath = path.join(dir, file);
    try {
      const def = loadWorkflowFromFile(filePath);
      const content = fs.readFileSync(filePath, 'utf-8');
      upsertWorkflow(def.name, def.description ?? null, content);
      console.log(`[workflow-loader] Loaded workflow: ${def.name}`);
    } catch (err) {
      console.error(`[workflow-loader] Failed to load ${file}:`, err);
    }
  }
}

export function getWorkflowDefinition(name: string): WorkflowDefinition {
  const record = listWorkflows().find(w => w.name === name);
  if (!record) {
    throw new Error(`Workflow not found: ${name}`);
  }
  const raw = yaml.load(record.yaml_content);
  const result = WorkflowSchema.safeParse(raw);
  if (!result.success) {
    throw new Error(`Invalid workflow ${name}: ${result.error.message}`);
  }
  return result.data;
}
