import cron from 'node-cron';
import { getActiveStepsWithTimeout, logStepEvent } from '../storage/repositories/step-repo';
import { TaskRunner } from './task-runner';

export class WakeScheduler {
  private runner: TaskRunner;
  private cronJob: cron.ScheduledTask | null = null;

  constructor(runner: TaskRunner) {
    this.runner = runner;
  }

  start(): void {
    // Check every 60 seconds for timed-out steps
    this.cronJob = cron.schedule('* * * * *', async () => {
      await this.checkTimeouts();
    });
    console.log('[wake-scheduler] Started timeout checker (every 60s)');
  }

  stop(): void {
    if (this.cronJob) {
      this.cronJob.stop();
      this.cronJob = null;
      console.log('[wake-scheduler] Stopped');
    }
  }

  async checkTimeouts(): Promise<void> {
    const activeSteps = getActiveStepsWithTimeout();
    const now = Date.now();

    for (const step of activeSteps) {
      if (!step.started_at || !step.timeout_seconds) continue;

      const startedAt = new Date(step.started_at).getTime();
      const elapsedSeconds = (now - startedAt) / 1000;

      if (elapsedSeconds > step.timeout_seconds) {
        console.warn(`[wake-scheduler] Step timed out: ${step.name} (${step.id}) — elapsed: ${Math.round(elapsedSeconds)}s, limit: ${step.timeout_seconds}s`);
        logStepEvent(step.id, 'error', `Step timed out after ${Math.round(elapsedSeconds)}s (limit: ${step.timeout_seconds}s)`);

        try {
          await this.runner.failStep(step.id, `Step timed out after ${Math.round(elapsedSeconds)}s`);
        } catch (err) {
          console.error(`[wake-scheduler] Error failing timed-out step ${step.id}: ${err}`);
        }
      }
    }
  }
}
