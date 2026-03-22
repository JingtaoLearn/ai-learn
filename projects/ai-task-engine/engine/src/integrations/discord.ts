import {
  Client,
  GatewayIntentBits,
  CategoryChannel,
  TextChannel,
  ChannelType,
  Guild,
} from 'discord.js';
import { STEP_STATUS_EMOJI } from '../engine/state-machine';
import type { StepStatus } from '../storage/repositories/step-repo';

let discordClient: Client | null = null;

export function getDiscordClient(): Client {
  if (discordClient) return discordClient;
  discordClient = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent,
    ],
  });
  return discordClient;
}

export async function loginDiscord(): Promise<void> {
  const token = process.env.DISCORD_BOT_TOKEN;
  if (!token) {
    throw new Error('DISCORD_BOT_TOKEN environment variable is required');
  }
  const client = getDiscordClient();
  if (client.isReady()) return;
  await client.login(token);
  await new Promise<void>(resolve => client.once('ready', () => resolve()));
  console.log(`[discord] Logged in as ${client.user?.tag}`);
}

export async function getGuild(): Promise<Guild> {
  const guildId = process.env.DISCORD_GUILD_ID;
  if (!guildId) {
    throw new Error('DISCORD_GUILD_ID environment variable is required');
  }
  const client = getDiscordClient();
  const guild = await client.guilds.fetch(guildId);
  return guild;
}

export async function createTaskCategory(taskId: string, description: string): Promise<CategoryChannel> {
  const guild = await getGuild();
  const shortId = taskId.substring(0, 8);
  const name = `📋 ${shortId}-${description}`.substring(0, 100);

  // Check if category already exists
  const existing = guild.channels.cache.find(
    c => c.type === ChannelType.GuildCategory && c.name === name
  ) as CategoryChannel | undefined;

  if (existing) return existing;

  return await guild.channels.create({
    name,
    type: ChannelType.GuildCategory,
  }) as CategoryChannel;
}

export async function createStepChannel(
  categoryId: string,
  stepIndex: number,
  stepName: string,
  status: StepStatus
): Promise<TextChannel> {
  const guild = await getGuild();
  const category = await guild.channels.fetch(categoryId) as CategoryChannel;

  const emoji = STEP_STATUS_EMOJI[status] || '⏸️';
  const safeName = stepName.toLowerCase().replace(/[^a-z0-9-]/g, '-').substring(0, 50);
  const channelName = `${emoji}s${stepIndex + 1}-${safeName}`;

  // Check if channel already exists in category
  const existing = category.children?.cache.find(c => {
    const stripped = c.name.replace(/^[^\w]+/, '');
    return stripped.startsWith(`s${stepIndex + 1}-${safeName}`);
  }) as TextChannel | undefined;

  if (existing) return existing;

  return await guild.channels.create({
    name: channelName,
    type: ChannelType.GuildText,
    parent: categoryId,
  }) as TextChannel;
}

export async function updateChannelStatusEmoji(channelId: string, status: StepStatus): Promise<void> {
  try {
    const guild = await getGuild();
    const channel = await guild.channels.fetch(channelId) as TextChannel;
    if (!channel) return;

    const emoji = STEP_STATUS_EMOJI[status] || '⏸️';
    // Replace leading emoji with new one
    const currentName = channel.name;
    const stripped = currentName.replace(/^[^\w]+/, '');
    await channel.setName(`${emoji}${stripped}`);
  } catch (err) {
    console.error(`[discord] Failed to update channel status emoji: ${err}`);
  }
}

export async function postToChannel(channelId: string, message: string): Promise<void> {
  try {
    const guild = await getGuild();
    const channel = await guild.channels.fetch(channelId) as TextChannel;
    if (!channel) {
      console.warn(`[discord] Channel ${channelId} not found`);
      return;
    }
    // Discord message limit is 2000 chars; split if needed
    const chunks = splitMessage(message, 2000);
    for (const chunk of chunks) {
      await channel.send(chunk);
    }
  } catch (err) {
    console.error(`[discord] Failed to post to channel ${channelId}: ${err}`);
  }
}

export function buildStepBriefMessage(
  stepIndex: number,
  stepName: string,
  goal: string,
  background: string | null,
  rules: string[],
  acceptanceType: string,
  acceptanceCriteria: string | null
): string {
  const lines = [
    `## Step ${stepIndex + 1}: ${stepName}`,
    '',
    `**Goal:** ${goal}`,
  ];

  if (background) {
    lines.push('', `**Background:**\n${background}`);
  }

  if (rules.length > 0) {
    lines.push('', `**Rules:**\n${rules.map(r => `• ${r}`).join('\n')}`);
  }

  lines.push('', `**Acceptance:** ${acceptanceType}`);
  if (acceptanceCriteria) {
    lines.push(`**Criteria:** ${acceptanceCriteria}`);
  }

  if (acceptanceType === 'human_confirm') {
    lines.push('', '> Type `!approve` in this channel to approve this step.');
  }

  return lines.join('\n');
}

function splitMessage(text: string, maxLen: number): string[] {
  if (text.length <= maxLen) return [text];
  const chunks: string[] = [];
  let i = 0;
  while (i < text.length) {
    chunks.push(text.slice(i, i + maxLen));
    i += maxLen;
  }
  return chunks;
}

export async function destroyDiscordClient(): Promise<void> {
  if (discordClient) {
    discordClient.destroy();
    discordClient = null;
  }
}
