import {
  StepExecutor, ExecutorInput, ExecutorResult, ExecutorOutput,
  ExecutorLoopConfig, DEFAULT_LOOP_CONFIG,
} from './interface';
import { postToChannel, pollForBotMessage, getLatestMessageId } from '../discord';

const OPENCLAW_GATEWAY_URL = process.env.OPENCLAW_GATEWAY_URL || 'http://127.0.0.1:18789';
const OPENCLAW_HOOKS_TOKEN = process.env.OPENCLAW_HOOKS_TOKEN || '';

export class OpenClawExecutor implements StepExecutor {
  name = 'openclaw';
  private config: ExecutorLoopConfig;

  constructor(config?: Partial<ExecutorLoopConfig>) {
    this.config = { ...DEFAULT_LOOP_CONFIG, ...config };
  }

  async execute(input: ExecutorInput): Promise<ExecutorResult> {
    console.log(`[openclaw-executor] Starting AI-driven execution for step: ${input.stepName}`);

    if (!input.discordChannelId) {
      return { success: false, error: 'OpenClaw executor requires a Discord channel.' };
    }
    if (!OPENCLAW_HOOKS_TOKEN) {
      return { success: false, error: 'OPENCLAW_HOOKS_TOKEN environment variable is not set.' };
    }

    const channelId = input.discordChannelId;

    try {
      // 1. Send initial work message to agent
      const initialPrompt = buildInitialPrompt(input);
      let snapshot = await getLatestMessageId(channelId);
      const sendResult = await this.sendToAgent(channelId, initialPrompt);
      if (!sendResult.ok) return sendResult.result;

      // 2. AI-driven execution loop
      for (let iteration = 1; iteration <= this.config.maxIterations; iteration++) {
        console.log(`[openclaw-executor] Iteration ${iteration}/${this.config.maxIterations}`);

        // Wait for agent's work response
        await sleep(this.config.evaluationDelay);
        const workResponse = await pollForBotMessage(
          channelId, snapshot, this.config.pollInterval, this.config.pollTimeout,
        );

        if (!workResponse) {
          return { success: false, error: `Agent did not respond within ${this.config.pollTimeout / 1000}s (iteration ${iteration})` };
        }

        console.log(`[openclaw-executor] Got agent response (${workResponse.content.length} chars)`);

        // If no acceptance criteria, first response is success
        if (!input.acceptanceCriteria) {
          return { success: true, output: buildOutput(workResponse.content) };
        }

        // 3. Ask agent to self-evaluate
        const evalSnapshot = await getLatestMessageId(channelId);
        const evalSend = await this.sendToAgent(channelId, buildEvaluationPrompt(input.acceptanceCriteria));
        if (!evalSend.ok) return evalSend.result;

        await sleep(this.config.evaluationDelay);
        const evalResponse = await pollForBotMessage(
          channelId, evalSnapshot, this.config.pollInterval, this.config.pollTimeout,
        );

        if (!evalResponse) {
          return { success: false, error: `Agent did not respond to evaluation prompt (iteration ${iteration})` };
        }

        const verdict = parseAcceptanceVerdict(evalResponse.content);

        if (verdict.pass) {
          console.log(`[openclaw-executor] ACCEPTANCE: PASS on iteration ${iteration}`);
          return {
            success: true,
            output: buildOutput(workResponse.content, { iterations: iteration }),
          };
        }

        console.log(`[openclaw-executor] ACCEPTANCE: FAIL — ${verdict.reason}`);

        // Last iteration — give up
        if (iteration >= this.config.maxIterations) break;

        // Send follow-up to continue working; next iteration will wait for the response
        await postToChannel(channelId, `🔄 **Iteration ${iteration}/${this.config.maxIterations}** — Continuing work...`);
        snapshot = await getLatestMessageId(channelId);
        const followUpSend = await this.sendToAgent(channelId, buildFollowUpPrompt(verdict.reason));
        if (!followUpSend.ok) return followUpSend.result;
        // Loop back — next iteration waits for the follow-up response
      }

      return {
        success: false,
        error: `Agent could not meet acceptance criteria after ${this.config.maxIterations} iterations. Escalating to human review.`,
      };
    } catch (err) {
      return {
        success: false,
        error: `OpenClaw executor error: ${err instanceof Error ? err.message : String(err)}`,
      };
    }
  }

  private async sendToAgent(
    channelId: string,
    text: string,
  ): Promise<{ ok: true } | { ok: false; result: ExecutorResult }> {
    const res = await fetch(`${OPENCLAW_GATEWAY_URL}/hooks/agent`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${OPENCLAW_HOOKS_TOKEN}`,
      },
      body: JSON.stringify({
        text,
        channel: 'discord',
        to: `channel:${channelId}`,
        sessionTarget: 'isolated',
        deliver: true,
      }),
    });

    if (!res.ok) {
      const body = await res.text();
      return {
        ok: false,
        result: { success: false, error: `OpenClaw hooks API error ${res.status}: ${body}` },
      };
    }

    return { ok: true };
  }
}

function buildInitialPrompt(input: ExecutorInput): string {
  const stepLabel = input.stepIndex !== undefined
    ? `Step ${input.stepIndex + 1}: ${input.stepName}`
    : `Step: ${input.stepName}`;

  const lines: string[] = [`## ${stepLabel}`, ''];
  lines.push(`**Goal:** ${input.goal}`, '');

  if (input.background) {
    lines.push('**Background:**', input.background, '');
  }

  if (input.previousOutput) {
    lines.push('**Previous Step Output:**', input.previousOutput, '');
  }

  if (input.rules.length > 0) {
    lines.push('**Rules:**');
    input.rules.forEach(r => lines.push(`- ${r}`));
    lines.push('');
  }

  if (input.acceptanceCriteria) {
    lines.push('**Acceptance Criteria:**', input.acceptanceCriteria, '');
  }

  if (input.retryContext) {
    lines.push('**Retry Context:**');
    lines.push(`- Retry attempt: ${input.retryContext.retryCount}`);
    if (input.retryContext.previousError) {
      lines.push(`- Previous error: ${input.retryContext.previousError}`);
    }
    lines.push('');
  }

  lines.push('Work on this goal now. When done, describe what you accomplished.');

  return lines.join('\n');
}

function buildEvaluationPrompt(criteria: string): string {
  return [
    'Review your work against these acceptance criteria:',
    '',
    criteria,
    '',
    'If ALL criteria are fully met, respond with exactly: ACCEPTANCE: PASS',
    'If any criteria are NOT met, respond with: ACCEPTANCE: FAIL — [what is missing]',
  ].join('\n');
}

function buildFollowUpPrompt(failReason: string): string {
  return `Continue working. ${failReason}`;
}

function parseAcceptanceVerdict(content: string): { pass: boolean; reason: string } {
  if (/ACCEPTANCE:\s*PASS/i.test(content)) {
    return { pass: true, reason: '' };
  }
  const failMatch = content.match(/ACCEPTANCE:\s*FAIL\s*[—\-]\s*(.*)/i);
  if (failMatch) {
    return { pass: false, reason: failMatch[1].trim() };
  }
  return { pass: false, reason: `Agent response did not contain clear verdict. Response: ${content.substring(0, 500)}` };
}

function buildOutput(
  agentResponse: string,
  metadata: Record<string, unknown> = {},
): ExecutorOutput {
  const summary = agentResponse.length > 500
    ? agentResponse.substring(0, 497) + '...'
    : agentResponse;

  return {
    summary,
    artifacts: [],
    metadata: { ...metadata, fullResponse: agentResponse },
    completedAt: new Date().toISOString(),
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}
