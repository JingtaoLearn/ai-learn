import 'dotenv/config';
import { syncWorkflowsFromDisk } from './workflow/loader';
import { TaskRunner } from './engine/task-runner';
import { WakeScheduler } from './engine/wake-scheduler';
import { createApiServer } from './api/server';
import { getDb } from './storage/db';
import { loginDiscord, destroyDiscordClient } from './integrations/discord';
import { startDiscordListener } from './integrations/discord-listener';

async function main(): Promise<void> {
  console.log('[ai-task-engine] Starting...');

  // Initialize database
  getDb();

  // Sync workflows from disk
  console.log('[ai-task-engine] Syncing workflows...');
  syncWorkflowsFromDisk();

  // Set up task runner and scheduler
  const runner = new TaskRunner();
  const scheduler = new WakeScheduler(runner);
  scheduler.start();

  // Start Discord listener if enabled
  const discordEnabled =
    process.env.DISCORD_ENABLED !== 'false' &&
    !!(process.env.DISCORD_BOT_TOKEN && process.env.DISCORD_GUILD_ID);

  if (discordEnabled) {
    try {
      await loginDiscord();
      startDiscordListener(runner);
      console.log('[ai-task-engine] Discord listener started');
    } catch (err) {
      console.error('[ai-task-engine] Discord listener failed to start (non-fatal):', err);
    }
  }

  // Start API server
  const port = parseInt(process.env.API_PORT || '3200', 10);
  const app = createApiServer(runner);
  app.listen(port, () => {
    console.log(`[ai-task-engine] API listening on http://localhost:${port}`);
  });

  // Graceful shutdown
  process.on('SIGINT', () => {
    console.log('\n[ai-task-engine] Shutting down...');
    scheduler.stop();
    if (discordEnabled) destroyDiscordClient();
    process.exit(0);
  });

  process.on('SIGTERM', () => {
    scheduler.stop();
    if (discordEnabled) destroyDiscordClient();
    process.exit(0);
  });
}

main().catch(err => {
  console.error('[ai-task-engine] Fatal error:', err);
  process.exit(1);
});
