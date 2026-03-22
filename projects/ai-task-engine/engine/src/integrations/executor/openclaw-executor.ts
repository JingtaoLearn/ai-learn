import { StepExecutor, ExecutorInput, ExecutorResult, ExecutorOutput } from './interface';
import { postToChannel, getDiscordClient } from '../discord';
import { TextChannel } from 'discord.js';

const OPENCLAW_GATEWAY_URL = process.env.OPENCLAW_GATEWAY_URL || 'http://127.0.0.1:18789';
const OPENCLAW_HOOKS_TOKEN = process.env.OPENCLAW_HOOKS_TOKEN || '';
const POLL_INTERVAL_MS = 10_000;
const DEFAULT_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes

export class OpenClawExecutor implements StepExecutor {
  name = 'openclaw';

  async execute(input: ExecutorInput): Promise<ExecutorResult> {
    console.log(`[openclaw-executor] Dispatching step to OpenClaw: ${input.stepName}`);

    if (!input.discordChannelId) {
      return {
        success: false,
        error: 'OpenClaw executor requires a Discord channel (discordChannelId is null). Ensure Discord is enabled.',
      };
    }

    if (!OPENCLAW_HOOKS_TOKEN) {
      return {
        success: false,
        error: 'OPENCLAW_HOOKS_TOKEN environment variable is not set.',
      };
    }

    const brief = buildStepBrief(input);

    try {
      // a) Post step brief to Discord channel
      await postToChannel(input.discordChannelId, brief);
      console.log(`[openclaw-executor] Posted step brief to channel ${input.discordChannelId}`);

      // b) Trigger OpenClaw agent session
      const hookResponse = await fetch(`${OPENCLAW_GATEWAY_URL}/hooks/agent`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${OPENCLAW_HOOKS_TOKEN}`,
        },
        body: JSON.stringify({
          text: brief,
          channel: 'discord',
          to: `channel:${input.discordChannelId}`,
          sessionTarget: 'isolated',
        }),
      });

      if (!hookResponse.ok) {
        const body = await hookResponse.text();
        return {
          success: false,
          error: `OpenClaw hooks API error ${hookResponse.status}: ${body}`,
        };
      }

      console.log(`[openclaw-executor] OpenClaw agent session triggered for channel ${input.discordChannelId}`);

      // c) Poll for completion
      const output = await pollForCompletion(input.discordChannelId, DEFAULT_TIMEOUT_MS);

      if (!output) {
        return {
          success: false,
          error: `Step timed out waiting for AI completion after ${DEFAULT_TIMEOUT_MS / 1000}s`,
        };
      }

      return { success: true, output };
    } catch (err) {
      return {
        success: false,
        error: `OpenClaw executor error: ${err instanceof Error ? err.message : String(err)}`,
      };
    }
  }
}

async function pollForCompletion(channelId: string, timeoutMs: number): Promise<ExecutorOutput | null> {
  const client = getDiscordClient();
  const deadline = Date.now() + timeoutMs;
  let afterSnowflake: string | undefined;

  // Snapshot current latest message so we only look at new messages
  try {
    const channel = await client.channels.fetch(channelId) as TextChannel;
    const messages = await channel.messages.fetch({ limit: 1 });
    if (messages.size > 0) {
      afterSnowflake = messages.first()!.id;
    }
  } catch (err) {
    console.warn(`[openclaw-executor] Could not snapshot channel messages: ${err}`);
  }

  while (Date.now() < deadline) {
    await sleep(POLL_INTERVAL_MS);

    try {
      const channel = await client.channels.fetch(channelId) as TextChannel;
      const fetchOptions: Parameters<typeof channel.messages.fetch>[0] = { limit: 50 };
      if (afterSnowflake) (fetchOptions as Record<string, unknown>).after = afterSnowflake;

      const messages = await channel.messages.fetch(fetchOptions);

      // Update afterSnowflake to the latest seen message
      for (const [, msg] of messages) {
        if (!afterSnowflake || msg.id > afterSnowflake) {
          afterSnowflake = msg.id;
        }

        if (msg.author.bot) {
          const parsed = tryParseCompletionOutput(msg.content);
          if (parsed) {
            console.log(`[openclaw-executor] Found completion output in message ${msg.id}`);
            return parsed;
          }
        }
      }
    } catch (err) {
      console.warn(`[openclaw-executor] Poll error (will retry): ${err}`);
    }
  }

  return null;
}

function tryParseCompletionOutput(content: string): ExecutorOutput | null {
  // Look for ```json ... ``` blocks
  const match = content.match(/```json\s*([\s\S]*?)```/);
  if (!match) return null;

  try {
    const parsed = JSON.parse(match[1].trim());
    if (
      typeof parsed.summary === 'string' &&
      Array.isArray(parsed.artifacts) &&
      typeof parsed.metadata === 'object' &&
      typeof parsed.completedAt === 'string'
    ) {
      return parsed as ExecutorOutput;
    }
  } catch {
    // Not valid JSON
  }
  return null;
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function buildStepBrief(input: ExecutorInput): string {
  const stepLabel = input.stepIndex !== undefined
    ? `Step ${input.stepIndex + 1}: ${input.stepName}`
    : `Step: ${input.stepName}`;

  const lines: string[] = [`## ${stepLabel}`, ''];
  lines.push(`**Goal:** ${input.goal}`, '');

  if (input.background) {
    lines.push('**Background:**');
    lines.push(input.background);
    lines.push('');
  }

  if (input.previousOutput) {
    lines.push('**Previous Step Output:**');
    lines.push(input.previousOutput);
    lines.push('');
  }

  if (input.rules.length > 0) {
    lines.push('**Rules:**');
    input.rules.forEach(r => lines.push(`- ${r}`));
    lines.push('');
  }

  if (input.retryContext) {
    lines.push('**Retry Context:**');
    lines.push(`- Retry attempt: ${input.retryContext.retryCount}`);
    if (input.retryContext.previousError) {
      lines.push(`- Previous error: ${input.retryContext.previousError}`);
    }
    lines.push('');
  }

  lines.push('**When you are done, post your results in this exact JSON format:**');
  lines.push('```json');
  lines.push(JSON.stringify({
    summary: 'What you accomplished',
    artifacts: [{ type: 'file', path: '...' }, { type: 'url', value: '...' }],
    metadata: {},
    completedAt: new Date().toISOString(),
  }, null, 2));
  lines.push('```');

  return lines.join('\n');
}
