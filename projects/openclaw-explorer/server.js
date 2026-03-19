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

// Skill directories
const SKILL_DIRS = {
  bundled: process.env.SKILLS_BUNDLED_DIR || '/data/skills-bundled',
  workspace: process.env.SKILLS_WORKSPACE_DIR || '/data/skills-workspace'
};

// Multi-agent sessions directories
const AGENT_SESSIONS = {
  zhang: { dir: process.env.OPENCLAW_SESSIONS_DIR || '/data/sessions', label: 'Little Zhang', emoji: '🔧' },
  customer: { dir: process.env.CUSTOMER_SESSIONS_DIR || '/data/customer-sessions', label: 'Little Customer', emoji: '🤝' },
};

// Discord channel ID to human-readable name mapping
// Loaded from external file (synced by OpenClaw cron) with in-memory cache
const CHANNEL_NAMES_FILE = process.env.CHANNEL_NAMES_FILE || '/data/channel-names.json';
let _channelNamesCache = {};
let _channelNamesMtime = 0;

function getChannelNames() {
  try {
    const stat = fs.statSync(CHANNEL_NAMES_FILE);
    if (stat.mtimeMs !== _channelNamesMtime) {
      _channelNamesCache = JSON.parse(fs.readFileSync(CHANNEL_NAMES_FILE, 'utf8'));
      _channelNamesMtime = stat.mtimeMs;
      console.log(`Loaded ${Object.keys(_channelNamesCache).length} channel names from ${CHANNEL_NAMES_FILE}`);
    }
  } catch (err) {
    if (Object.keys(_channelNamesCache).length === 0) {
      console.warn(`Channel names file not found: ${CHANNEL_NAMES_FILE}`);
    }
  }
  return _channelNamesCache;
}

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
app.get('/api/agents', (req, res) => {
  const agents = Object.entries(AGENT_SESSIONS).map(([id, cfg]) => ({
    id, label: cfg.label, emoji: cfg.emoji,
  }));
  res.json(agents);
});

app.get('/api/channel-names', (req, res) => {
  res.json(getChannelNames());
});

function getAgentSessionsDir(agentId) {
  const agent = AGENT_SESSIONS[agentId];
  return agent ? agent.dir : AGENT_SESSIONS.zhang.dir;
}

function computeTags(key, sessionData) {
  const tags = [];
  const channelNames = getChannelNames();
  if (key.includes('discord:channel:')) {
    tags.push('discord');
    const match = key.match(/discord:channel:(\d+)/);
    if (match && channelNames[match[1]]) {
      tags.push(channelNames[match[1]]);
    }
  } else if (key.includes('cron:')) {
    tags.push('cron');
    if (sessionData.label) {
      const name = sessionData.label.replace(/^Cron:\s*/, '');
      tags.push(name);
    }
    if (key.includes(':run:')) tags.push('run');
  } else if (key.includes('hook:')) {
    tags.push('hook');
  } else if (key.includes('direct:')) {
    tags.push('dm');
  }
  return tags;
}

app.get('/api/sessions', (req, res) => {
  const agentId = req.query.agent || 'zhang';
  const sessionsDir = getAgentSessionsDir(agentId);
  const sessionsFile = path.join(sessionsDir, 'sessions.json');
  try {
    const data = JSON.parse(fs.readFileSync(sessionsFile, 'utf8'));
    const sessions = Object.entries(data).map(([key, value]) => ({
      key,
      sessionId: value.sessionId,
      updatedAt: value.updatedAt,
      label: value.label || null,
      tags: computeTags(key, value)
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
  const agentId = req.query.agent || 'zhang';
  const sessionsDir = getAgentSessionsDir(agentId);
  // Sanitize session ID to prevent path traversal
  const id = path.basename(req.params.id).replace(/[^a-zA-Z0-9_-]/g, '');
  if (!id) {
    return res.status(400).json({ error: 'Invalid session ID' });
  }
  const sessionFile = path.join(sessionsDir, `${id}.jsonl`);
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

// --- Skill Viewer API ---

function parseSkillFrontmatter(content) {
  const match = content.match(/^---\n([\s\S]*?)\n---/);
  if (!match) return { meta: {}, body: content };

  const yamlStr = match[1];
  const body = content.slice(match[0].length).trim();
  const meta = {};

  // Simple YAML parser for flat + nested metadata
  // Handles: key: value, key: "value", key: { json }
  for (const line of yamlStr.split('\n')) {
    const kv = line.match(/^(\w+):\s*(.*)/);
    if (!kv) continue;
    const key = kv[1];
    let val = kv[2].trim();
    // Remove surrounding quotes
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    // Try JSON parse for object values
    if (val.startsWith('{') || val.startsWith('[')) {
      try { val = JSON.parse(val); } catch { /* keep as string */ }
    }
    meta[key] = val;
  }

  return { meta, body };
}

function readSkillDir(dirPath, source) {
  const skills = [];
  try {
    const entries = fs.readdirSync(dirPath, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const name = entry.name;
      const skillPath = path.join(dirPath, name);
      const skillMdPath = path.join(skillPath, 'SKILL.md');

      let meta = {};
      let hasSkillMd = false;
      try {
        const content = fs.readFileSync(skillMdPath, 'utf8');
        hasSkillMd = true;
        const parsed = parseSkillFrontmatter(content);
        meta = parsed.meta;
      } catch { /* no SKILL.md or unreadable */ }

      const ocMeta = (meta.metadata && meta.metadata.openclaw) || {};

      // Check for references/ and scripts/ subdirectories
      let hasReferences = false;
      let hasScripts = false;
      try {
        hasReferences = fs.statSync(path.join(skillPath, 'references')).isDirectory();
      } catch { /* ignore */ }
      try {
        hasScripts = fs.statSync(path.join(skillPath, 'scripts')).isDirectory();
      } catch { /* ignore */ }

      skills.push({
        name: meta.name || name,
        description: meta.description || '',
        emoji: ocMeta.emoji || '',
        source,
        requires: ocMeta.requires || null,
        hasReferences,
        hasScripts,
      });
    }
  } catch { /* directory doesn't exist or unreadable */ }
  return skills;
}

app.get('/api/skills', (req, res) => {
  const bundled = readSkillDir(SKILL_DIRS.bundled, 'bundled');
  const workspace = readSkillDir(SKILL_DIRS.workspace, 'workspace');
  const all = [...bundled, ...workspace].sort((a, b) => a.name.localeCompare(b.name));
  res.json(all);
});

app.get('/api/skills/:name', (req, res) => {
  const name = path.basename(req.params.name);
  if (!name) {
    return res.status(400).json({ error: 'Invalid skill name' });
  }

  // Search in both directories
  for (const [source, dir] of Object.entries(SKILL_DIRS)) {
    const skillPath = path.join(dir, name);
    try {
      const stat = fs.statSync(skillPath);
      if (!stat.isDirectory()) continue;
    } catch { continue; }

    const skillMdPath = path.join(skillPath, 'SKILL.md');
    let meta = {};
    let body = '';
    try {
      const content = fs.readFileSync(skillMdPath, 'utf8');
      const parsed = parseSkillFrontmatter(content);
      meta = parsed.meta;
      body = parsed.body;
    } catch { /* no SKILL.md */ }

    const ocMeta = (meta.metadata && meta.metadata.openclaw) || {};

    // List files recursively (shallow - one level of subdirs)
    const files = [];
    function listFiles(dir, prefix) {
      try {
        const entries = fs.readdirSync(dir, { withFileTypes: true });
        for (const e of entries) {
          const rel = prefix ? prefix + '/' + e.name : e.name;
          if (e.isFile()) {
            files.push(rel);
          } else if (e.isDirectory()) {
            listFiles(path.join(dir, e.name), rel);
          }
        }
      } catch { /* ignore */ }
    }
    listFiles(skillPath, '');

    return res.json({
      name: meta.name || name,
      description: meta.description || '',
      emoji: ocMeta.emoji || '',
      source,
      requires: ocMeta.requires || null,
      content: body,
      files,
    });
  }

  res.status(404).json({ error: 'Skill not found' });
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`OpenClaw Explorer running on port ${PORT}`);
});
