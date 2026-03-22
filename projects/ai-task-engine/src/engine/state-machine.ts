import { StepStatus } from '../storage/repositories/step-repo';
import { TaskStatus } from '../storage/repositories/task-repo';

export type StepStatusTransition = {
  from: StepStatus;
  to: StepStatus;
  guard?: string;
};

// Valid transitions for steps
const VALID_STEP_TRANSITIONS: Array<[StepStatus, StepStatus]> = [
  ['pending', 'active'],
  ['active', 'executing'],
  ['executing', 'validating'],
  ['validating', 'completed'],
  ['validating', 'failed'],
  ['executing', 'failed'],
  ['active', 'failed'],
  ['failed', 'retrying'],
  ['retrying', 'active'],
  ['active', 'blocked'],
  ['executing', 'blocked'],
  ['blocked', 'active'],
  ['pending', 'failed'], // direct fail for immediate error
  ['active', 'completed'], // for auto-complete paths
  ['executing', 'completed'], // for auto-complete paths
];

const VALID_TASK_TRANSITIONS: Array<[TaskStatus, TaskStatus]> = [
  ['pending', 'active'],
  ['active', 'completed'],
  ['active', 'cancelled'],
  ['active', 'failed'],
  ['pending', 'cancelled'],
];

export function canTransitionStep(from: StepStatus, to: StepStatus): boolean {
  return VALID_STEP_TRANSITIONS.some(([f, t]) => f === from && t === to);
}

export function canTransitionTask(from: TaskStatus, to: TaskStatus): boolean {
  return VALID_TASK_TRANSITIONS.some(([f, t]) => f === from && t === to);
}

export function assertStepTransition(from: StepStatus, to: StepStatus): void {
  if (!canTransitionStep(from, to)) {
    throw new Error(`Invalid step transition: ${from} → ${to}`);
  }
}

export function assertTaskTransition(from: TaskStatus, to: TaskStatus): void {
  if (!canTransitionTask(from, to)) {
    throw new Error(`Invalid task transition: ${from} → ${to}`);
  }
}

export function isTerminalStepStatus(status: StepStatus): boolean {
  return status === 'completed' || status === 'failed';
}

export function isTerminalTaskStatus(status: TaskStatus): boolean {
  return status === 'completed' || status === 'cancelled' || status === 'failed';
}

// Status emoji mapping for Discord channel names
export const STEP_STATUS_EMOJI: Record<StepStatus, string> = {
  pending: '⏸️',
  active: '⏳',
  executing: '⏳',
  validating: '⏳',
  completed: '✅',
  failed: '❌',
  blocked: '🚫',
  retrying: '⏳',
};
