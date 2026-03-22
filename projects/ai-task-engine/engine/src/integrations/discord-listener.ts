import { Events, Message } from 'discord.js';
import { getDiscordClient } from './discord';
import { getStepByDiscordChannelId } from '../storage/repositories/step-repo';
import { postToChannel } from './discord';
import { TaskRunner } from '../engine/task-runner';

export function startDiscordListener(runner: TaskRunner): void {
  const client = getDiscordClient();

  client.on(Events.MessageCreate, async (message: Message) => {
    // Ignore bot messages
    if (message.author.bot) return;

    const channelId = message.channelId;
    const content = message.content.trim();

    if (!content.startsWith('!approve') && !content.startsWith('!reject')) return;

    // Look up which step owns this channel
    const step = getStepByDiscordChannelId(channelId);
    if (!step) return;

    if (content.startsWith('!approve')) {
      try {
        await runner.approveStep(step.task_id, step.id);
        await postToChannel(channelId, `✅ Step approved by ${message.author.username}.`);
      } catch (err) {
        await postToChannel(channelId, `⚠️ Could not approve step: ${err instanceof Error ? err.message : String(err)}`);
      }
      return;
    }

    if (content.startsWith('!reject')) {
      const reason = content.replace(/^!reject\s*/, '').trim() || 'No reason provided';
      try {
        await runner.rejectStep(step.task_id, step.id, reason);
        await postToChannel(channelId, `❌ Step rejected by ${message.author.username}: ${reason}`);
      } catch (err) {
        await postToChannel(channelId, `⚠️ Could not reject step: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
  });

  console.log('[discord-listener] Listening for !approve and !reject commands');
}
