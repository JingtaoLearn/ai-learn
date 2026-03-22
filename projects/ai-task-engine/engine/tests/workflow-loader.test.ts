import path from 'path';
import fs from 'fs';
import os from 'os';
import yaml from 'js-yaml';
import { WorkflowSchema, parseTimeout } from '../src/workflow/schema';
import { loadWorkflowFromFile } from '../src/workflow/loader';

describe('WorkflowSchema', () => {
  test('validates a minimal valid workflow', () => {
    const raw = {
      name: 'test-workflow',
      steps: [
        {
          name: 'step-one',
          goal: 'Do something useful',
          acceptance: { type: 'human_confirm' },
        },
      ],
    };
    const result = WorkflowSchema.safeParse(raw);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.name).toBe('test-workflow');
      expect(result.data.steps).toHaveLength(1);
      expect(result.data.steps[0].maxRetries).toBe(0);
      expect(result.data.steps[0].wakePolicy).toBe('dependency');
    }
  });

  test('validates a full workflow with all fields', () => {
    const raw = {
      name: 'full-workflow',
      description: 'A complete workflow',
      steps: [
        {
          name: 'analyze',
          goal: 'Analyze the problem',
          background: 'Some background',
          rules: ['no-code-changes', 'document-findings'],
          acceptance: { type: 'human_confirm' },
          timeout: '30m',
          wakePolicy: 'dependency',
          maxRetries: 2,
        },
        {
          name: 'implement',
          goal: 'Implement the solution',
          acceptance: {
            type: 'ai_self_check',
            criteria: 'Code compiles and tests pass',
          },
          timeout: '60m',
        },
        {
          name: 'test',
          goal: 'Run tests',
          acceptance: {
            type: 'automated',
            command: 'npm test',
          },
        },
      ],
    };
    const result = WorkflowSchema.safeParse(raw);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.steps).toHaveLength(3);
      expect(result.data.steps[0].rules).toEqual(['no-code-changes', 'document-findings']);
      expect(result.data.steps[1].acceptance.type).toBe('ai_self_check');
    }
  });

  test('rejects workflow with no steps', () => {
    const raw = { name: 'empty', steps: [] };
    const result = WorkflowSchema.safeParse(raw);
    expect(result.success).toBe(false);
  });

  test('rejects workflow with missing name', () => {
    const raw = { steps: [{ name: 's1', goal: 'g', acceptance: { type: 'human_confirm' } }] };
    const result = WorkflowSchema.safeParse(raw);
    expect(result.success).toBe(false);
  });

  test('rejects step with missing goal', () => {
    const raw = {
      name: 'test',
      steps: [{ name: 's1', acceptance: { type: 'human_confirm' } }],
    };
    const result = WorkflowSchema.safeParse(raw);
    expect(result.success).toBe(false);
  });

  test('rejects ai_self_check without criteria', () => {
    const raw = {
      name: 'test',
      steps: [{ name: 's1', goal: 'g', acceptance: { type: 'ai_self_check' } }],
    };
    const result = WorkflowSchema.safeParse(raw);
    expect(result.success).toBe(false);
  });

  test('rejects automated without command', () => {
    const raw = {
      name: 'test',
      steps: [{ name: 's1', goal: 'g', acceptance: { type: 'automated' } }],
    };
    const result = WorkflowSchema.safeParse(raw);
    expect(result.success).toBe(false);
  });

  test('rejects invalid acceptance type', () => {
    const raw = {
      name: 'test',
      steps: [{ name: 's1', goal: 'g', acceptance: { type: 'invalid_type' } }],
    };
    const result = WorkflowSchema.safeParse(raw);
    expect(result.success).toBe(false);
  });
});

describe('parseTimeout', () => {
  test('parses seconds', () => {
    expect(parseTimeout('30s')).toBe(30);
  });

  test('parses minutes', () => {
    expect(parseTimeout('30m')).toBe(1800);
  });

  test('parses hours', () => {
    expect(parseTimeout('2h')).toBe(7200);
  });

  test('parses days', () => {
    expect(parseTimeout('1d')).toBe(86400);
  });

  test('returns undefined for invalid format', () => {
    expect(parseTimeout('invalid')).toBeUndefined();
    expect(parseTimeout('30x')).toBeUndefined();
  });
});

describe('loadWorkflowFromFile', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'workflow-test-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true });
  });

  test('loads a valid workflow YAML file', () => {
    const workflowData = {
      name: 'test-workflow',
      description: 'Test',
      steps: [
        {
          name: 'step-one',
          goal: 'Do something',
          acceptance: { type: 'human_confirm' },
        },
      ],
    };
    const filePath = path.join(tmpDir, 'test.yaml');
    fs.writeFileSync(filePath, yaml.dump(workflowData));

    const result = loadWorkflowFromFile(filePath);
    expect(result.name).toBe('test-workflow');
    expect(result.steps).toHaveLength(1);
  });

  test('throws on invalid workflow YAML', () => {
    const filePath = path.join(tmpDir, 'invalid.yaml');
    fs.writeFileSync(filePath, yaml.dump({ name: 'invalid', steps: [] }));
    expect(() => loadWorkflowFromFile(filePath)).toThrow();
  });

  test('throws on malformed YAML', () => {
    const filePath = path.join(tmpDir, 'malformed.yaml');
    fs.writeFileSync(filePath, 'not: valid: yaml: [[[');
    expect(() => loadWorkflowFromFile(filePath)).toThrow();
  });
});
