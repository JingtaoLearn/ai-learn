import { syncWorkflowsFromDisk } from './workflow/loader';
import { TaskRunner } from './engine/task-runner';
import { WakeScheduler } from './engine/wake-scheduler';
import { createApiServer } from './api/server';
import { getDb } from './storage/db';

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
    process.exit(0);
  });

  process.on('SIGTERM', () => {
    scheduler.stop();
    process.exit(0);
  });
}

main().catch(err => {
  console.error('[ai-task-engine] Fatal error:', err);
  process.exit(1);
});
