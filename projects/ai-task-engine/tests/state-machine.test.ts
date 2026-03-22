import {
  canTransitionStep,
  canTransitionTask,
  assertStepTransition,
  assertTaskTransition,
  isTerminalStepStatus,
  isTerminalTaskStatus,
  STEP_STATUS_EMOJI,
} from '../src/engine/state-machine';

describe('State Machine — Step Transitions', () => {
  test('allows valid step transitions', () => {
    expect(canTransitionStep('pending', 'active')).toBe(true);
    expect(canTransitionStep('active', 'executing')).toBe(true);
    expect(canTransitionStep('executing', 'validating')).toBe(true);
    expect(canTransitionStep('validating', 'completed')).toBe(true);
    expect(canTransitionStep('validating', 'failed')).toBe(true);
    expect(canTransitionStep('failed', 'retrying')).toBe(true);
    expect(canTransitionStep('retrying', 'active')).toBe(true);
    expect(canTransitionStep('active', 'blocked')).toBe(true);
    expect(canTransitionStep('blocked', 'active')).toBe(true);
  });

  test('rejects invalid step transitions', () => {
    expect(canTransitionStep('completed', 'active')).toBe(false);
    expect(canTransitionStep('completed', 'failed')).toBe(false);
    expect(canTransitionStep('pending', 'completed')).toBe(false);
    expect(canTransitionStep('pending', 'executing')).toBe(false);
  });

  test('assertStepTransition throws on invalid transition', () => {
    expect(() => assertStepTransition('completed', 'active')).toThrow('Invalid step transition');
    expect(() => assertStepTransition('pending', 'validating')).toThrow('Invalid step transition');
  });

  test('assertStepTransition does not throw on valid transition', () => {
    expect(() => assertStepTransition('pending', 'active')).not.toThrow();
    expect(() => assertStepTransition('active', 'executing')).not.toThrow();
  });

  test('isTerminalStepStatus identifies terminal states', () => {
    expect(isTerminalStepStatus('completed')).toBe(true);
    expect(isTerminalStepStatus('failed')).toBe(true);
    expect(isTerminalStepStatus('active')).toBe(false);
    expect(isTerminalStepStatus('pending')).toBe(false);
    expect(isTerminalStepStatus('executing')).toBe(false);
    expect(isTerminalStepStatus('blocked')).toBe(false);
  });
});

describe('State Machine — Task Transitions', () => {
  test('allows valid task transitions', () => {
    expect(canTransitionTask('pending', 'active')).toBe(true);
    expect(canTransitionTask('active', 'completed')).toBe(true);
    expect(canTransitionTask('active', 'cancelled')).toBe(true);
    expect(canTransitionTask('active', 'failed')).toBe(true);
    expect(canTransitionTask('pending', 'cancelled')).toBe(true);
  });

  test('rejects invalid task transitions', () => {
    expect(canTransitionTask('completed', 'active')).toBe(false);
    expect(canTransitionTask('cancelled', 'active')).toBe(false);
    expect(canTransitionTask('failed', 'active')).toBe(false);
    expect(canTransitionTask('pending', 'completed')).toBe(false);
  });

  test('assertTaskTransition throws on invalid transition', () => {
    expect(() => assertTaskTransition('completed', 'active')).toThrow('Invalid task transition');
  });

  test('isTerminalTaskStatus identifies terminal states', () => {
    expect(isTerminalTaskStatus('completed')).toBe(true);
    expect(isTerminalTaskStatus('cancelled')).toBe(true);
    expect(isTerminalTaskStatus('failed')).toBe(true);
    expect(isTerminalTaskStatus('active')).toBe(false);
    expect(isTerminalTaskStatus('pending')).toBe(false);
  });
});

describe('STEP_STATUS_EMOJI', () => {
  test('has emoji for all step statuses', () => {
    const statuses = ['pending', 'active', 'executing', 'validating', 'completed', 'failed', 'blocked', 'retrying'];
    statuses.forEach(status => {
      expect(STEP_STATUS_EMOJI[status as keyof typeof STEP_STATUS_EMOJI]).toBeDefined();
    });
  });

  test('completed uses checkmark emoji', () => {
    expect(STEP_STATUS_EMOJI['completed']).toBe('✅');
  });

  test('failed uses cross emoji', () => {
    expect(STEP_STATUS_EMOJI['failed']).toBe('❌');
  });

  test('blocked uses no entry emoji', () => {
    expect(STEP_STATUS_EMOJI['blocked']).toBe('🚫');
  });
});
