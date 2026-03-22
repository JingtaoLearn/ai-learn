import express, { Request, Response, NextFunction } from 'express';
import jwt from 'jsonwebtoken';
import session from 'express-session';
import { TaskRunner } from '../engine/task-runner';
import { listTasks, getTask } from '../storage/repositories/task-repo';
import { getStepsByTask, getStepLogs } from '../storage/repositories/step-repo';
import { listWorkflows } from '../storage/repositories/workflow-repo';

declare module 'express-session' {
  interface SessionData {
    user: { email: string; name: string };
  }
}

const AUTH_SHARED_SECRET = process.env.AUTH_SHARED_SECRET || '';
const SESSION_SECRET = process.env.SESSION_SECRET || 'changeme';
const LOGIN_URL = process.env.LOGIN_URL || 'https://ms-login.ai.jingtao.fun/auth/login';
const VIRTUAL_HOST = process.env.VIRTUAL_HOST || 'task.ai.jingtao.fun';
const SELF_CALLBACK = `https://${VIRTUAL_HOST}/auth/callback`;

function requireAuth(req: Request, res: Response, next: NextFunction): void {
  // Allow if session authenticated
  if (req.session.user) {
    next();
    return;
  }
  // Fallback: Bearer token auth
  const token = process.env.API_AUTH_TOKEN;
  if (token) {
    const authHeader = req.headers['authorization'];
    if (authHeader === `Bearer ${token}`) {
      next();
      return;
    }
  }
  res.status(401).json({ error: 'Not authenticated' });
}

export function createApiServer(runner: TaskRunner): express.Application {
  const app = express();

  app.set('trust proxy', 1);
  app.use(express.json());
  app.use(express.urlencoded({ extended: true }));

  app.use(session({
    secret: SESSION_SECRET,
    resave: false,
    saveUninitialized: false,
    cookie: {
      secure: true,
      httpOnly: true,
      maxAge: 7 * 24 * 60 * 60 * 1000,
    },
  }));

  // --- Auth routes (no auth required) ---

  app.get('/auth/config', (_req: Request, res: Response) => {
    const loginUrl = `${LOGIN_URL}?redirect=${encodeURIComponent(SELF_CALLBACK)}`;
    res.json({ loginUrl });
  });

  app.post('/auth/callback', (req: Request, res: Response) => {
    const { token } = req.body as { token?: string };
    if (!token) {
      res.status(400).send('Token required');
      return;
    }
    if (!AUTH_SHARED_SECRET) {
      res.status(500).send('Auth not configured');
      return;
    }
    try {
      const decoded = jwt.verify(token, AUTH_SHARED_SECRET) as { email?: string; displayName?: string; name?: string };
      const email = (decoded.email || '').toLowerCase();
      req.session.user = { email, name: decoded.displayName || decoded.name || email };
      res.redirect('/');
    } catch {
      res.status(401).send('Invalid or expired token');
    }
  });

  app.get('/auth/me', (req: Request, res: Response) => {
    if (req.session.user) {
      res.json({ authenticated: true, user: req.session.user });
    } else {
      res.json({ authenticated: false });
    }
  });

  app.get('/auth/logout', (req: Request, res: Response) => {
    req.session.destroy(() => {
      res.json({ ok: true });
    });
  });

  // --- Public API routes ---

  app.get('/api/health', (_req: Request, res: Response) => {
    res.json({ status: 'ok', timestamp: new Date().toISOString() });
  });

  // --- Protected API routes ---

  app.use('/api', requireAuth);

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
