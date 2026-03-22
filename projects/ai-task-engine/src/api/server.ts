import express, { Request, Response, NextFunction } from 'express';
import { TaskRunner } from '../engine/task-runner';
import { listTasks, getTask } from '../storage/repositories/task-repo';
import { getStepsByTask, getStepLogs } from '../storage/repositories/step-repo';
import { listWorkflows } from '../storage/repositories/workflow-repo';

export function createApiServer(runner: TaskRunner): express.Application {
  const app = express();
  app.use(express.json());

  // Health check
  app.get('/api/health', (_req: Request, res: Response) => {
    res.json({ status: 'ok', timestamp: new Date().toISOString() });
  });

  // Workflow endpoints
  app.get('/api/workflows', (_req: Request, res: Response) => {
    const workflows = listWorkflows();
    res.json(workflows.map(w => ({
      id: w.id,
      name: w.name,
      description: w.description,
      created_at: w.created_at,
    })));
  });

  // Task endpoints
  app.post('/api/tasks', async (req: Request, res: Response, next: NextFunction) => {
    try {
      const { workflow, name, description } = req.body as { workflow: string; name: string; description?: string };
      if (!workflow || !name) {
        res.status(400).json({ error: 'workflow and name are required' });
        return;
      }
      const task = await runner.createAndStartTask(workflow, name, description);
      const steps = getStepsByTask(task.id);
      res.status(201).json({ ...task, steps });
    } catch (err) {
      next(err);
    }
  });

  app.get('/api/tasks', (req: Request, res: Response) => {
    const status = req.query.status as string | undefined;
    const tasks = listTasks(status as any);
    res.json(tasks);
  });

  app.get('/api/tasks/:id', (req: Request, res: Response) => {
    const task = getTask(req.params.id);
    if (!task) {
      res.status(404).json({ error: 'Task not found' });
      return;
    }
    const steps = getStepsByTask(task.id);
    res.json({ ...task, steps });
  });

  app.post('/api/tasks/:id/steps/:stepId/approve', async (req: Request, res: Response, next: NextFunction) => {
    try {
      await runner.approveStep(req.params.id, req.params.stepId);
      res.json({ success: true });
    } catch (err) {
      next(err);
    }
  });

  app.post('/api/tasks/:id/steps/:stepId/resume', async (req: Request, res: Response, next: NextFunction) => {
    try {
      await runner.resumeStep(req.params.id, req.params.stepId);
      res.json({ success: true });
    } catch (err) {
      next(err);
    }
  });

  app.post('/api/tasks/:id/cancel', async (req: Request, res: Response, next: NextFunction) => {
    try {
      await runner.cancelTask(req.params.id);
      res.json({ success: true });
    } catch (err) {
      next(err);
    }
  });

  app.get('/api/tasks/:id/steps/:stepId/logs', (req: Request, res: Response) => {
    const logs = getStepLogs(req.params.stepId);
    res.json(logs);
  });

  // Error handler
  app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
    console.error('[api] Error:', err.message);
    res.status(500).json({ error: err.message });
  });

  return app;
}
