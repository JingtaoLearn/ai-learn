#!/usr/bin/env node
import { Command } from 'commander';
import { syncWorkflowsFromDisk } from '../workflow/loader';
import { listWorkflows } from '../storage/repositories/workflow-repo';
import { listTasks, getTask } from '../storage/repositories/task-repo';
import { getStepsByTask } from '../storage/repositories/step-repo';
import { TaskRunner } from '../engine/task-runner';
import { createApiServer } from '../api/server';
import { WakeScheduler } from '../engine/wake-scheduler';

// Initialize DB on import
import { getDb } from '../storage/db';
getDb();

const program = new Command();

program
  .name('task-engine')
  .description('AI Task-Driven Assistant System CLI')
  .version('0.1.0');

// Start daemon
program
  .command('start')
  .description('Start the task engine daemon (API server + wake scheduler)')
  .option('-p, --port <port>', 'API port', process.env.API_PORT || '3200')
  .action(async (options: { port: string }) => {
    const port = parseInt(options.port, 10);
    console.log('[daemon] Syncing workflows from disk...');
    syncWorkflowsFromDisk();

    const runner = new TaskRunner();
    const scheduler = new WakeScheduler(runner);
    scheduler.start();

    const app = createApiServer(runner);
    app.listen(port, () => {
      console.log(`[daemon] API server listening on http://localhost:${port}`);
      console.log('[daemon] Ready. Press Ctrl+C to stop.');
    });

    process.on('SIGINT', () => {
      console.log('\n[daemon] Shutting down...');
      scheduler.stop();
      process.exit(0);
    });
  });

// Workflow commands
const workflowCmd = program.command('workflow').description('Manage workflow templates');

workflowCmd
  .command('list')
  .description('List available workflow templates')
  .action(() => {
    syncWorkflowsFromDisk();
    const workflows = listWorkflows();
    if (workflows.length === 0) {
      console.log('No workflows found. Add YAML files to the workflows/ directory.');
      return;
    }
    console.log('\nAvailable workflows:');
    workflows.forEach(w => {
      console.log(`  • ${w.name}${w.description ? ` — ${w.description}` : ''}`);
    });
    console.log();
  });

// Task commands
const taskCmd = program.command('task').description('Manage tasks');

taskCmd
  .command('create')
  .description('Create and start a new task')
  .requiredOption('-w, --workflow <name>', 'Workflow name')
  .requiredOption('-n, --name <name>', 'Task name')
  .option('-d, --description <desc>', 'Task description')
  .action(async (options: { workflow: string; name: string; description?: string }) => {
    syncWorkflowsFromDisk();
    const runner = new TaskRunner();
    try {
      console.log(`Creating task: ${options.name} (workflow: ${options.workflow})`);
      const task = await runner.createAndStartTask(options.workflow, options.name, options.description);
      console.log(`\nTask created: ${task.id}`);
      console.log(`Status: ${task.status}`);
      const steps = getStepsByTask(task.id);
      console.log('\nSteps:');
      steps.forEach(s => {
        console.log(`  [${s.status}] ${s.step_index + 1}. ${s.name}`);
      });
    } catch (err) {
      console.error('Error:', err instanceof Error ? err.message : err);
      process.exit(1);
    }
  });

taskCmd
  .command('list')
  .description('List all tasks')
  .option('-s, --status <status>', 'Filter by status')
  .action((options: { status?: string }) => {
    const tasks = listTasks(options.status as any);
    if (tasks.length === 0) {
      console.log('No tasks found.');
      return;
    }
    console.log('\nTasks:');
    tasks.forEach(t => {
      console.log(`  [${t.status}] ${t.id.substring(0, 8)}... ${t.name}`);
    });
    console.log();
  });

taskCmd
  .command('status <taskId>')
  .description('Show detailed task status')
  .action((taskId: string) => {
    const task = getTask(taskId);
    if (!task) {
      console.error(`Task not found: ${taskId}`);
      process.exit(1);
    }
    console.log(`\nTask: ${task.name}`);
    console.log(`ID: ${task.id}`);
    console.log(`Status: ${task.status}`);
    console.log(`Created: ${task.created_at}`);
    const steps = getStepsByTask(task.id);
    console.log('\nSteps:');
    steps.forEach(s => {
      const timeout = s.timeout_seconds ? ` (timeout: ${s.timeout_seconds}s)` : '';
      const retries = s.max_retries > 0 ? ` [retry: ${s.retry_count}/${s.max_retries}]` : '';
      console.log(`  [${s.status}] ${s.step_index + 1}. ${s.name} — ${s.acceptance_type}${timeout}${retries}`);
      if (s.error_message) {
        console.log(`    Error: ${s.error_message}`);
      }
    });
    console.log();
  });

taskCmd
  .command('cancel <taskId>')
  .description('Cancel a task')
  .action(async (taskId: string) => {
    const runner = new TaskRunner();
    try {
      await runner.cancelTask(taskId);
      console.log(`Task cancelled: ${taskId}`);
    } catch (err) {
      console.error('Error:', err instanceof Error ? err.message : err);
      process.exit(1);
    }
  });

// Step commands
const stepCmd = program.command('step').description('Manage steps');

stepCmd
  .command('approve <taskId> <stepId>')
  .description('Approve a human-confirmation step')
  .action(async (taskId: string, stepId: string) => {
    const runner = new TaskRunner();
    try {
      await runner.approveStep(taskId, stepId);
      console.log('Step approved successfully.');
    } catch (err) {
      console.error('Error:', err instanceof Error ? err.message : err);
      process.exit(1);
    }
  });

program.parse(process.argv);
