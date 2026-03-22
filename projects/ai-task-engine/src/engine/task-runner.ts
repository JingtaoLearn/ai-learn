import { getWorkflowDefinition } from '../workflow/loader';
import { parseTimeout } from '../workflow/schema';
import { createTask, getTask, updateTask, TaskRecord } from '../storage/repositories/task-repo';
import {
  createStep, getStep, getStepsByTask, updateStep, logStepEvent,
  StepRecord, StepOutput,
} from '../storage/repositories/step-repo';
import { getWorkflowByName } from '../storage/repositories/workflow-repo';
import { assertStepTransition, assertTaskTransition, isTerminalStepStatus } from './state-machine';
import { emsCheck } from '../integrations/ems';
import {
  loginDiscord, createTaskCategory, createStepChannel, postToChannel,
  updateChannelStatusEmoji, buildStepBriefMessage,
} from '../integrations/discord';
import { StepExecutor } from '../integrations/executor/interface';
import { MockExecutor } from '../integrations/executor/mock-executor';
import { OpenClawExecutor } from '../integrations/executor/openclaw-executor';

function getExecutor(): StepExecutor {
  const mode = process.env.EXECUTOR_MODE || 'mock';
  if (mode === 'openclaw') return new OpenClawExecutor();
  return new MockExecutor();
}

export class TaskRunner {
  private executor: StepExecutor;
  private discordEnabled: boolean;

  constructor() {
    this.executor = getExecutor();
    const discordEnabledEnv = process.env.DISCORD_ENABLED;
    if (discordEnabledEnv === 'false') {
      this.discordEnabled = false;
    } else {
      this.discordEnabled = !!(process.env.DISCORD_BOT_TOKEN && process.env.DISCORD_GUILD_ID);
    }
  }

  async createAndStartTask(workflowName: string, name: string, description?: string): Promise<TaskRecord> {
    // Ensure workflow exists in DB
    const workflowRecord = getWorkflowByName(workflowName);
    if (!workflowRecord) {
      throw new Error(`Workflow not found: ${workflowName}. Run sync first.`);
    }

    const def = getWorkflowDefinition(workflowName);
    const task = createTask(workflowRecord.id, name, description);

    // Create steps in DB
    for (let i = 0; i < def.steps.length; i++) {
      const stepDef = def.steps[i];
      createStep({
        taskId: task.id,
        stepIndex: i,
        name: stepDef.name,
        goal: stepDef.goal,
        background: stepDef.background,
        rules: stepDef.rules,
        acceptanceType: stepDef.acceptance.type,
        acceptanceCriteria: 'criteria' in stepDef.acceptance ? stepDef.acceptance.criteria : undefined,
        acceptanceCommand: 'command' in stepDef.acceptance ? stepDef.acceptance.command : undefined,
        timeoutSeconds: stepDef.timeout ? parseTimeout(stepDef.timeout) : undefined,
        wakePolicy: stepDef.wakePolicy,
        maxRetries: stepDef.maxRetries,
      });
    }

    // Start the task
    assertTaskTransition('pending', 'active');
    updateTask(task.id, { status: 'active' });

    console.log(`[task-runner] Task created: ${task.id} (${name})`);

    // Set up Discord category if enabled
    if (this.discordEnabled) {
      try {
        await loginDiscord();
        const safeDesc = name.toLowerCase().replace(/[^a-z0-9-]/g, '-').substring(0, 30);
        const category = await createTaskCategory(task.id, safeDesc);
        updateTask(task.id, { discord_category_id: category.id });
        console.log(`[task-runner] Created Discord category: ${category.name}`);
      } catch (err) {
        console.error(`[task-runner] Discord setup failed (non-fatal): ${err}`);
      }
    }

    // Start the first step
    await this.advanceTask(task.id);
    return getTask(task.id)!;
  }

  async advanceTask(taskId: string): Promise<void> {
    const task = getTask(taskId);
    if (!task) throw new Error(`Task not found: ${taskId}`);

    const steps = getStepsByTask(taskId);
    const nextStep = steps.find(s => s.status === 'pending');

    if (!nextStep) {
      // All steps done — check if all completed
      const allCompleted = steps.every(s => s.status === 'completed');
      if (allCompleted) {
        assertTaskTransition(task.status, 'completed');
        updateTask(taskId, { status: 'completed' });
        console.log(`[task-runner] Task completed: ${taskId}`);
      }
      return;
    }

    await this.activateStep(nextStep);
  }

  async activateStep(step: StepRecord): Promise<void> {
    console.log(`[task-runner] Activating step: ${step.name} (${step.id})`);

    // EMS check
    const rules = step.rules_json ? JSON.parse(step.rules_json) as string[] : [];
    const emsResult = await emsCheck(step.goal, rules);
    if (!emsResult.available) {
      logStepEvent(step.id, 'ems_check', 'EMS unavailable, proceeding with caution');
    } else {
      logStepEvent(step.id, 'ems_check', `EMS verdict: ${emsResult.verdict}`, { reason: emsResult.reason });
      if (emsResult.verdict === 'block') {
        updateStep(step.id, { status: 'blocked', error_message: `EMS blocked: ${emsResult.reason}` });
        console.warn(`[task-runner] Step blocked by EMS: ${step.name} — ${emsResult.reason}`);
        return;
      }
      if (emsResult.verdict === 'warn') {
        console.warn(`[task-runner] EMS warning for step ${step.name}: ${emsResult.reason}`);
      }
    }

    // Transition to active
    assertStepTransition(step.status, 'active');
    updateStep(step.id, {
      status: 'active',
      started_at: new Date().toISOString(),
    });
    logStepEvent(step.id, 'status_change', 'Step activated');

    // Set up Discord channel if enabled
    const task = getTask(step.task_id)!;
    if (this.discordEnabled && task.discord_category_id) {
      try {
        const channel = await createStepChannel(task.discord_category_id, step.step_index, step.name, 'active');
        updateStep(step.id, { discord_channel_id: channel.id });
        const brief = buildStepBriefMessage(
          step.step_index, step.name, step.goal, step.background,
          rules, step.acceptance_type, step.acceptance_criteria
        );
        await postToChannel(channel.id, brief);
      } catch (err) {
        console.error(`[task-runner] Discord channel setup failed (non-fatal): ${err}`);
      }
    }

    // Execute based on acceptance type
    if (step.acceptance_type === 'human_confirm') {
      console.log(`[task-runner] Step ${step.name} waiting for human !approve`);
      // Will be advanced by approve endpoint / Discord message handler
    } else if (step.acceptance_type === 'ai_self_check') {
      await this.executeAiStep(step);
    } else if (step.acceptance_type === 'automated') {
      await this.executeAutomatedStep(step);
    }
  }

  async executeAiStep(step: StepRecord): Promise<void> {
    assertStepTransition(step.status, 'executing');
    updateStep(step.id, { status: 'executing' });
    logStepEvent(step.id, 'status_change', 'Step executing via AI');

    // Build previous output context
    const steps = getStepsByTask(step.task_id);
    const prevStep = steps.find(s => s.step_index === step.step_index - 1);
    const previousOutput = prevStep?.output_json ?? null;

    const retryContext = step.retry_count > 0 ? {
      retryCount: step.retry_count,
      previousError: step.error_message,
      previousOutput: step.output_json,
    } : undefined;

    try {
      const result = await this.executor.execute({
        stepId: step.id,
        taskId: step.task_id,
        stepIndex: step.step_index,
        stepName: step.name,
        goal: step.goal,
        background: step.background,
        rules: step.rules_json ? JSON.parse(step.rules_json) : [],
        discordChannelId: step.discord_channel_id,
        previousOutput,
        retryContext,
      });

      if (result.success && result.output) {
        await this.completeStep(step.id, result.output);
      } else {
        await this.failStep(step.id, result.error || 'Executor returned failure');
      }
    } catch (err) {
      await this.failStep(step.id, `Executor error: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async executeAutomatedStep(step: StepRecord): Promise<void> {
    if (!step.acceptance_command) {
      await this.failStep(step.id, 'No acceptance command defined for automated step');
      return;
    }

    const { exec } = await import('child_process');
    const { promisify } = await import('util');
    const execAsync = promisify(exec);

    assertStepTransition(step.status, 'executing');
    updateStep(step.id, { status: 'executing' });
    logStepEvent(step.id, 'status_change', `Running automated command: ${step.acceptance_command}`);

    try {
      const { stdout, stderr } = await execAsync(step.acceptance_command);
      const output: StepOutput = {
        summary: `Automated command succeeded: ${step.acceptance_command}`,
        artifacts: [],
        metadata: { stdout, stderr },
        completedAt: new Date().toISOString(),
      };
      await this.completeStep(step.id, output);
    } catch (err: unknown) {
      const error = err as { message: string; code?: number };
      await this.failStep(step.id, `Automated command failed (exit ${error.code}): ${error.message}`);
    }
  }

  async completeStep(stepId: string, output: StepOutput): Promise<void> {
    const step = getStep(stepId);
    if (!step) throw new Error(`Step not found: ${stepId}`);

    assertStepTransition(step.status, 'completed');
    updateStep(stepId, {
      status: 'completed',
      output_json: JSON.stringify(output),
      completed_at: new Date().toISOString(),
    });
    logStepEvent(stepId, 'status_change', 'Step completed', { summary: output.summary });

    if (this.discordEnabled && step.discord_channel_id) {
      try {
        await updateChannelStatusEmoji(step.discord_channel_id, 'completed');
        await postToChannel(step.discord_channel_id, `✅ **Step completed**\n${output.summary}`);
      } catch (err) {
        console.error(`[task-runner] Discord update failed (non-fatal): ${err}`);
      }
    }

    console.log(`[task-runner] Step completed: ${step.name}`);

    // Dependency wake: advance to next step
    await this.advanceTask(step.task_id);
  }

  async failStep(stepId: string, error: string): Promise<void> {
    const step = getStep(stepId);
    if (!step) return;

    if (step.retry_count < step.max_retries) {
      updateStep(stepId, {
        status: 'retrying',
        retry_count: step.retry_count + 1,
        error_message: error,
      });
      logStepEvent(stepId, 'error', `Step failed (retry ${step.retry_count + 1}/${step.max_retries}): ${error}`);
      console.warn(`[task-runner] Retrying step: ${step.name} (${step.retry_count + 1}/${step.max_retries})`);

      // Brief backoff before retry
      await new Promise(resolve => setTimeout(resolve, 2000));

      const refreshed = getStep(stepId)!;
      updateStep(stepId, { status: 'pending' });
      await this.activateStep({ ...refreshed, status: 'pending' });
    } else {
      updateStep(stepId, { status: 'failed', error_message: error });
      logStepEvent(stepId, 'error', `Step failed permanently: ${error}`);
      console.error(`[task-runner] Step failed: ${step.name} — ${error}`);

      if (this.discordEnabled && step.discord_channel_id) {
        try {
          await updateChannelStatusEmoji(step.discord_channel_id, 'failed');
          await postToChannel(step.discord_channel_id, `❌ **Step failed:** ${error}`);
        } catch (err) {
          console.error(`[task-runner] Discord update failed (non-fatal): ${err}`);
        }
      }

      // Fail the task
      const task = getTask(step.task_id)!;
      if (task.status === 'active') {
        updateTask(step.task_id, { status: 'failed' });
      }
    }
  }

  async approveStep(taskId: string, stepId: string): Promise<void> {
    const step = getStep(stepId);
    if (!step) throw new Error(`Step not found: ${stepId}`);
    if (step.task_id !== taskId) throw new Error('Step does not belong to task');
    if (step.acceptance_type !== 'human_confirm') {
      throw new Error(`Step ${step.name} does not require human confirmation`);
    }
    if (!['active', 'executing', 'validating'].includes(step.status)) {
      throw new Error(`Step ${step.name} is not in an approvable state (status: ${step.status})`);
    }

    logStepEvent(stepId, 'human_input', 'Human approved step');
    const output: StepOutput = {
      summary: `Human approved step: ${step.name}`,
      artifacts: [],
      metadata: { approvedByHuman: true },
      completedAt: new Date().toISOString(),
    };
    await this.completeStep(stepId, output);
  }

  async rejectStep(taskId: string, stepId: string, reason: string): Promise<void> {
    const step = getStep(stepId);
    if (!step) throw new Error(`Step not found: ${stepId}`);
    if (step.task_id !== taskId) throw new Error('Step does not belong to task');

    logStepEvent(stepId, 'human_input', `Human rejected step: ${reason}`);
    updateStep(stepId, { status: 'failed', error_message: `Rejected by human: ${reason}` });

    if (this.discordEnabled && step.discord_channel_id) {
      try {
        await updateChannelStatusEmoji(step.discord_channel_id, 'failed');
        await postToChannel(step.discord_channel_id, `❌ **Step rejected:** ${reason}`);
      } catch (err) {
        console.error(`[task-runner] Discord update failed (non-fatal): ${err}`);
      }
    }

    const task = getTask(step.task_id)!;
    if (task.status === 'active') {
      updateTask(step.task_id, { status: 'failed' });
    }

    console.log(`[task-runner] Step rejected: ${step.name} — ${reason}`);
  }

  async cancelTask(taskId: string): Promise<void> {
    const task = getTask(taskId);
    if (!task) throw new Error(`Task not found: ${taskId}`);
    assertTaskTransition(task.status, 'cancelled');
    updateTask(taskId, { status: 'cancelled' });

    const steps = getStepsByTask(taskId);
    for (const step of steps) {
      if (!isTerminalStepStatus(step.status)) {
        updateStep(step.id, { status: 'failed', error_message: 'Task cancelled' });
        if (this.discordEnabled && step.discord_channel_id) {
          try {
            await updateChannelStatusEmoji(step.discord_channel_id, 'failed');
          } catch { /* non-fatal */ }
        }
      }
    }
    console.log(`[task-runner] Task cancelled: ${taskId}`);
  }

  async resumeStep(taskId: string, stepId: string): Promise<void> {
    const step = getStep(stepId);
    if (!step) throw new Error(`Step not found: ${stepId}`);
    if (step.task_id !== taskId) throw new Error('Step does not belong to task');

    if (!['failed', 'blocked'].includes(step.status)) {
      throw new Error(`Step ${step.name} cannot be resumed from status: ${step.status}`);
    }

    updateStep(stepId, { status: 'pending', error_message: null });
    logStepEvent(stepId, 'status_change', 'Step resumed by human');
    await this.activateStep({ ...step, status: 'pending' });
  }
}
