const express = require('express');
const path = require('path');
const fs = require('fs');
const jwt = require('jsonwebtoken');
const session = require('express-session');

const app = express();
const PORT = process.env.PORT || 80;

const AUTH_SHARED_SECRET = process.env.AUTH_SHARED_SECRET || 'changeme';
const SESSION_SECRET = process.env.SESSION_SECRET || 'changeme';
const LOGIN_URL = process.env.LOGIN_URL || 'https://ms-login.ai.jingtao.fun/auth/login';
const SELF_CALLBACK = process.env.SELF_CALLBACK || 'https://oc.ai.jingtao.fun/auth/callback';
const ALLOWED_EMAILS = (process.env.ALLOWED_EMAILS || '851207685@qq.com').split(',').map(e => e.trim().toLowerCase());
const SESSIONS_DIR = process.env.OPENCLAW_SESSIONS_DIR || '/data/sessions';

// Discord channel ID to human-readable name mapping
const CHANNEL_NAMES = {
  '1471415769702338693': '#project-todo-list',
  '1471417838597046282': '#general',
  '1471431883111006228': '#ai-collab',
  '1471435262704877731': '#travel',
  '1471440043020255307': '#test',
  '1471447483799441512': '#life',
  '1471461335047868447': '#maintain',
  '1471473007556956285': '#auto-issue-dev',
  '1471477049108729926': '#zhang-logs',
  '1471492750007599201': '#project-wedding',
  '1471527108990861312': '#market-watch',
  '1471661524069122159': '#music',
  '1471686661116264632': '#automation-logs',
  '1471752874982768794': '#brainstorm',
  '1471820146988548097': '#coding-tasks',
  '1476763875939717294': '#skill-lab',
  '1476778639860305970': '#security',
  '1476782643763875910': '#ideas',
  '1477655095653957693': '#project-listen-english',
  '1478245402250842294': '#blog',
  '1479462804934103100': '#project-openclaw-intro'
};

app.set('trust proxy', 1);

app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use(session({
  secret: SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: {
    secure: process.env.NODE_ENV === 'production',
    httpOnly: true,
    maxAge: 7 * 24 * 60 * 60 * 1000
  }
}));

app.use(express.static(path.join(__dirname, 'public')));

// Auth callback - receives form POST from ms-login with JWT token
app.post('/auth/callback', (req, res) => {
  const { token } = req.body;
  if (!token) {
    return res.status(400).send('Token required');
  }
  try {
    const decoded = jwt.verify(token, AUTH_SHARED_SECRET);
    const email = (decoded.email || '').toLowerCase();
    if (!ALLOWED_EMAILS.includes(email)) {
      return res.status(403).send('Email not authorized');
    }
    req.session.user = { email, name: decoded.displayName || decoded.name || email };
    // Redirect to session viewer after successful auth
    res.redirect('/pages/session-viewer.html');
  } catch (err) {
    res.status(401).send('Invalid or expired token');
  }
});

app.get('/auth/logout', (req, res) => {
  req.session.destroy(() => {
    res.json({ ok: true });
  });
});

app.get('/auth/me', (req, res) => {
  if (req.session.user) {
    res.json({ authenticated: true, user: req.session.user });
  } else {
    res.json({ authenticated: false });
  }
});

// Auth middleware for /api/* routes
function requireAuth(req, res, next) {
  if (!req.session.user) {
    return res.status(401).json({ error: 'Not authenticated' });
  }
  next();
}

app.use('/api', requireAuth);

// API routes
app.get('/api/channel-names', (req, res) => {
  res.json(CHANNEL_NAMES);
});

app.get('/api/sessions', (req, res) => {
  const sessionsFile = path.join(SESSIONS_DIR, 'sessions.json');
  try {
    const data = JSON.parse(fs.readFileSync(sessionsFile, 'utf8'));
    const sessions = Object.entries(data).map(([key, value]) => ({
      key,
      sessionId: value.sessionId,
      updatedAt: value.updatedAt,
      label: value.label || null
    }));
    sessions.sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt));
    res.json(sessions);
  } catch (err) {
    if (err.code === 'ENOENT') {
      return res.json([]);
    }
    res.status(500).json({ error: 'Failed to read sessions' });
  }
});

app.get('/api/sessions/:id', (req, res) => {
  // Sanitize session ID to prevent path traversal
  const id = path.basename(req.params.id).replace(/[^a-zA-Z0-9_-]/g, '');
  if (!id) {
    return res.status(400).json({ error: 'Invalid session ID' });
  }
  const sessionFile = path.join(SESSIONS_DIR, `${id}.jsonl`);
  try {
    const content = fs.readFileSync(sessionFile, 'utf8');
    const lines = content.split('\n').filter(line => line.trim());
    const events = [];
    for (const line of lines) {
      try {
        events.push(JSON.parse(line));
      } catch {
        // Skip malformed lines
      }
    }
    res.json(events);
  } catch (err) {
    if (err.code === 'ENOENT') {
      return res.status(404).json({ error: 'Session not found' });
    }
    res.status(500).json({ error: 'Failed to read session' });
  }
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`OpenClaw Explorer running on port ${PORT}`);
});
