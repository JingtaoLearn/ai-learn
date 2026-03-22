import { canTransitionStep, canTransitionTask } from '../src/engine/state-machine';

// Task runner tests focus on state machine integration
// Full integration tests require SQLite setup

describe('Task Runner — State Machine Integration', () => {
  test('task lifecycle: pending → active → completed', () => {
    expect(canTransitionTask('pending', 'active')).toBe(true);
    expect(canTransitionTask('active', 'completed')).toBe(true);
  });

  test('task can be cancelled from pending or active', () => {
    expect(canTransitionTask('pending', 'cancelled')).toBe(true);
    expect(canTransitionTask('active', 'cancelled')).toBe(true);
  });

  test('task cannot be reactivated after completion', () => {
    expect(canTransitionTask('completed', 'active')).toBe(false);
    expect(canTransitionTask('cancelled', 'active')).toBe(false);
  });

  test('step lifecycle: pending → active → executing → completed', () => {
    expect(canTransitionStep('pending', 'active')).toBe(true);
    expect(canTransitionStep('active', 'executing')).toBe(true);
    expect(canTransitionStep('executing', 'validating')).toBe(true);
    expect(canTransitionStep('validating', 'completed')).toBe(true);
  });

  test('step can be retried after failure', () => {
    expect(canTransitionStep('executing', 'failed')).toBe(true);
    expect(canTransitionStep('failed', 'retrying')).toBe(true);
    expect(canTransitionStep('retrying', 'active')).toBe(true);
  });

  test('step can be blocked and unblocked', () => {
    expect(canTransitionStep('active', 'blocked')).toBe(true);
    expect(canTransitionStep('executing', 'blocked')).toBe(true);
    expect(canTransitionStep('blocked', 'active')).toBe(true);
  });

  test('completed step cannot transition to any state', () => {
    const invalidTargets = ['pending', 'active', 'executing', 'validating', 'failed', 'blocked', 'retrying'];
    invalidTargets.forEach(target => {
      expect(canTransitionStep('completed', target as any)).toBe(false);
    });
  });
});
