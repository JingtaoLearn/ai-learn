import { StepExecutor, ExecutorInput, ExecutorResult } from './interface';

const OPENCLAW_GATEWAY_URL = process.env.OPENCLAW_GATEWAY_URL || 'http://127.0.0.1:18789';

/**
 * OpenClaw Gateway Executor
 *
 * Triggers step execution via the OpenClaw gateway.
 * Each step channel is mapped to an isolated OpenClaw agent session.
 *
 * The engine triggers execution by posting the step brief (goal + background + rules)
 * to the channel. OpenClaw picks it up as an agent turn in that channel's session.
 * Step completion is signaled when the AI posts a structured JSON output block.
 *
 * TODO (Phase 2): Implement actual OpenClaw gateway integration:
 * 1. POST step brief to OpenClaw session endpoint
 * 2. Poll or subscribe to completion events
 * 3. Parse structured JSON output from agent response
 * 4. Handle multi-turn conversations within a step session
 */
export class OpenClawExecutor implements StepExecutor {
  name = 'openclaw';

  async execute(input: ExecutorInput): Promise<ExecutorResult> {
    console.log(`[openclaw-executor] Dispatching step to OpenClaw: ${input.stepName}`);
    console.log(`[openclaw-executor] Gateway URL: ${OPENCLAW_GATEWAY_URL}`);

    const sessionId = input.discordChannelId
      ? `task-${input.taskId}-step-${input.stepId}`
      : `step-${input.stepId}`;

    const brief = buildStepBrief(input);

    try {
      // TODO: POST to OpenClaw gateway to create/resume agent session
      // const response = await fetch(`${OPENCLAW_GATEWAY_URL}/api/sessions/${sessionId}/turns`, {
      //   method: 'POST',
      //   headers: { 'Content-Type': 'application/json' },
      //   body: JSON.stringify({ message: brief, discordChannelId: input.discordChannelId }),
      // });
      //
      // TODO: Poll for completion or subscribe to webhook
      // const result = await waitForCompletion(sessionId);
      //
      // TODO: Parse structured output block from AI response

      console.warn(`[openclaw-executor] OpenClaw integration not yet implemented. Session: ${sessionId}`);
      console.warn(`[openclaw-executor] Step brief would be: ${brief.substring(0, 200)}...`);

      return {
        success: false,
        error: 'OpenClaw executor is not yet fully implemented. Use EXECUTOR_MODE=mock for testing.',
      };
    } catch (err) {
      return {
        success: false,
        error: `OpenClaw gateway error: ${err instanceof Error ? err.message : String(err)}`,
      };
    }
  }
}

function buildStepBrief(input: ExecutorInput): string {
  const lines: string[] = [
    `# Step: ${input.stepName}`,
    '',
    `## Goal`,
    input.goal,
    '',
  ];

  if (input.background) {
    lines.push('## Background');
    lines.push(input.background);
    lines.push('');
  }

  if (input.rules.length > 0) {
    lines.push('## Rules / Constraints');
    input.rules.forEach(r => lines.push(`- ${r}`));
    lines.push('');
  }

  if (input.retryContext) {
    lines.push('## Retry Context');
    lines.push(`Retry attempt: ${input.retryContext.retryCount}`);
    if (input.retryContext.previousError) {
      lines.push(`Previous error: ${input.retryContext.previousError}`);
    }
    lines.push('');
  }

  lines.push('## Output Format');
  lines.push('When complete, post a JSON block in this exact format:');
  lines.push('```json');
  lines.push(JSON.stringify({
    summary: 'Brief description of what was done',
    artifacts: [],
    metadata: {},
    completedAt: new Date().toISOString(),
  }, null, 2));
  lines.push('```');

  return lines.join('\n');
}
