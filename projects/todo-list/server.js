const express = require('express');
const session = require('express-session');
const BetterSqlite3SessionStore = require('better-sqlite3-session-store');
const Database = require('better-sqlite3');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const jwksRsa = require('jwks-rsa');
const path = require('path');
const crypto = require('crypto');

const app = express();
const PORT = process.env.PORT || 80;
const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'data', 'todos.db');
const SESSION_SECRET = process.env.SESSION_SECRET || crypto.randomBytes(32).toString('hex');

// Microsoft Auth (Azure AD) configuration
const MS_CLIENT_ID = process.env.MS_CLIENT_ID || '';
const MS_TENANT_ID = process.env.MS_TENANT_ID || 'common';

// JWKS client for verifying Microsoft tokens
const jwksClient = jwksRsa({
  jwksUri: `https://login.microsoftonline.com/${MS_TENANT_ID}/discovery/v2.0/keys`,
  cache: true,
  cacheMaxAge: 86400000,
});

// Ensure data directory exists
const fs = require('fs');
fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });

// Initialize SQLite database
const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');

// Users table
db.exec(`
  CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
  )
`);

// Todos table
db.exec(`
  CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    priority TEXT NOT NULL DEFAULT 'medium',
    due_date TEXT DEFAULT NULL,
    user_id INTEGER REFERENCES users(id)
  )
`);

// Migration: add priority column if it doesn't exist
try {
  db.exec(`ALTER TABLE todos ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'`);
} catch (e) {
  // Column already exists, ignore
}

// Migration: add due_date column if it doesn't exist
try {
  db.exec(`ALTER TABLE todos ADD COLUMN due_date TEXT DEFAULT NULL`);
} catch (e) {
  // Column already exists, ignore
}

// Migration: add user_id column if it doesn't exist
try {
  db.exec(`ALTER TABLE todos ADD COLUMN user_id INTEGER REFERENCES users(id)`);
} catch (e) {
  // Column already exists, ignore
}

// Migration: add microsoft_id column to users for Microsoft auth
try {
  db.exec(`ALTER TABLE users ADD COLUMN microsoft_id TEXT UNIQUE`);
} catch (e) {
  // Column already exists, ignore
}

// Migration: add email column to users
try {
  db.exec(`ALTER TABLE users ADD COLUMN email TEXT`);
} catch (e) {
  // Column already exists, ignore
}

// Migration: make password_hash nullable (Microsoft users don't have passwords)
// SQLite doesn't support ALTER COLUMN, but the NOT NULL constraint only applies to new inserts.
// We handle this by using a default empty string for Microsoft-only users.

// API keys table for agent-friendly API
db.exec(`
  CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    name TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT DEFAULT NULL
  )
`);

// Tags table and junction table
db.exec(`
  CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
  )
`);
db.exec(`
  CREATE TABLE IF NOT EXISTS todo_tags (
    todo_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (todo_id, tag_id),
    FOREIGN KEY (todo_id) REFERENCES todos(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
  )
`);

// Input sanitization
const MAX_TEXT_LENGTH = 500;
function sanitize(str) {
  return str.replace(/[<>&"']/g, c => ({
    '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

// Helper: get tags for a todo
function getTagsForTodo(todoId) {
  return db.prepare(`
    SELECT t.id, t.name FROM tags t
    JOIN todo_tags tt ON tt.tag_id = t.id
    WHERE tt.todo_id = ?
  `).all(todoId);
}

// Helper: sync tags for a todo
function syncTags(todoId, tagNames) {
  db.prepare('DELETE FROM todo_tags WHERE todo_id = ?').run(todoId);
  if (!tagNames || !tagNames.length) return;
  const insertTag = db.prepare('INSERT OR IGNORE INTO tags (name) VALUES (?)');
  const getTag = db.prepare('SELECT id FROM tags WHERE name = ?');
  const linkTag = db.prepare('INSERT OR IGNORE INTO todo_tags (todo_id, tag_id) VALUES (?, ?)');
  for (const name of tagNames) {
    const trimmed = name.trim().toLowerCase().slice(0, 50);
    if (!trimmed) continue;
    insertTag.run(trimmed);
    const tag = getTag.get(trimmed);
    if (tag) linkTag.run(todoId, tag.id);
  }
}

// Middleware
app.use(express.json());

// Session setup
const SqliteStore = BetterSqlite3SessionStore(session);
app.use(session({
  store: new SqliteStore({ client: db, expired: { clear: true, intervalMs: 900000 } }),
  secret: SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: {
    httpOnly: true,
    sameSite: 'lax',
    maxAge: 7 * 24 * 60 * 60 * 1000, // 7 days
  },
}));

app.use(express.static(path.join(__dirname, 'public')));

// Auth middleware - protects all /api routes except auth endpoints
function requireAuth(req, res, next) {
  if (req.session && req.session.userId) {
    return next();
  }
  res.status(401).json({ error: 'Authentication required' });
}

// Combined auth: session OR API key
function requireAnyAuth(req, res, next) {
  // Check session first
  if (req.session && req.session.userId) {
    req.authUserId = req.session.userId;
    return next();
  }
  // Check API key
  const authHeader = req.headers.authorization;
  if (authHeader && authHeader.startsWith('Bearer ')) {
    const apiKey = authHeader.slice(7);
    const keyHash = crypto.createHash('sha256').update(apiKey).digest('hex');
    const keyRecord = db.prepare('SELECT id, user_id FROM api_keys WHERE key_hash = ?').get(keyHash);
    if (keyRecord) {
      db.prepare('UPDATE api_keys SET last_used_at = datetime(\'now\') WHERE id = ?').run(keyRecord.id);
      req.authUserId = keyRecord.user_id;
      return next();
    }
  }
  res.status(401).json({ error: 'Authentication required' });
}

// Helper: verify Microsoft ID token
function verifyMicrosoftToken(token) {
  return new Promise((resolve, reject) => {
    const getKey = (header, callback) => {
      jwksClient.getSigningKey(header.kid, (err, key) => {
        if (err) return callback(err);
        callback(null, key.getPublicKey());
      });
    };
    const options = {
      audience: MS_CLIENT_ID,
      issuer: [
        `https://login.microsoftonline.com/${MS_TENANT_ID}/v2.0`,
        `https://sts.windows.net/${MS_TENANT_ID}/`,
      ],
      algorithms: ['RS256'],
    };
    // If tenant is 'common', accept any issuer from Microsoft
    if (MS_TENANT_ID === 'common') {
      delete options.issuer;
    }
    jwt.verify(token, getKey, options, (err, decoded) => {
      if (err) return reject(err);
      resolve(decoded);
    });
  });
}

// Helper: find or create user from Microsoft identity
function findOrCreateMicrosoftUser(microsoftId, email, displayName) {
  let user = db.prepare('SELECT id, username FROM users WHERE microsoft_id = ?').get(microsoftId);
  if (user) return user;

  // Generate a username from email or displayName
  let baseUsername = (email ? email.split('@')[0] : displayName || 'user')
    .toLowerCase().replace(/[^a-z0-9_-]/g, '-').slice(0, 40);
  let username = baseUsername;
  let suffix = 1;
  while (db.prepare('SELECT id FROM users WHERE username = ?').get(username)) {
    username = baseUsername + '-' + suffix;
    suffix++;
  }

  const result = db.prepare(
    'INSERT INTO users (username, password_hash, microsoft_id, email) VALUES (?, ?, ?, ?)'
  ).run(username, '', microsoftId, email || null);
  return { id: result.lastInsertRowid, username };
}

// --- Auth routes ---

// POST /api/auth/register
app.post('/api/auth/register', (req, res) => {
  const { username, password } = req.body;
  if (!username || typeof username !== 'string' || !username.trim()) {
    return res.status(400).json({ error: 'Username is required' });
  }
  if (!password || typeof password !== 'string' || password.length < 6) {
    return res.status(400).json({ error: 'Password must be at least 6 characters' });
  }
  const trimmedUsername = username.trim().toLowerCase();
  if (trimmedUsername.length > 50) {
    return res.status(400).json({ error: 'Username must be 50 characters or fewer' });
  }
  if (!/^[a-z0-9_-]+$/.test(trimmedUsername)) {
    return res.status(400).json({ error: 'Username can only contain letters, numbers, hyphens, and underscores' });
  }
  const existing = db.prepare('SELECT id FROM users WHERE username = ?').get(trimmedUsername);
  if (existing) {
    return res.status(409).json({ error: 'Username already taken' });
  }
  const passwordHash = bcrypt.hashSync(password, 10);
  const result = db.prepare('INSERT INTO users (username, password_hash) VALUES (?, ?)').run(trimmedUsername, passwordHash);
  req.session.userId = result.lastInsertRowid;
  req.session.username = trimmedUsername;
  res.status(201).json({ username: trimmedUsername });
});

// POST /api/auth/login
app.post('/api/auth/login', (req, res) => {
  const { username, password } = req.body;
  if (!username || !password) {
    return res.status(400).json({ error: 'Username and password are required' });
  }
  const trimmedUsername = username.trim().toLowerCase();
  const user = db.prepare('SELECT id, username, password_hash FROM users WHERE username = ?').get(trimmedUsername);
  if (!user || !bcrypt.compareSync(password, user.password_hash)) {
    return res.status(401).json({ error: 'Invalid username or password' });
  }
  req.session.userId = user.id;
  req.session.username = user.username;
  res.json({ username: user.username });
});

// POST /api/auth/logout
app.post('/api/auth/logout', (req, res) => {
  req.session.destroy(() => {
    res.json({ ok: true });
  });
});

// GET /api/auth/me - check current session
app.get('/api/auth/me', (req, res) => {
  if (req.session && req.session.userId) {
    return res.json({ username: req.session.username });
  }
  res.status(401).json({ error: 'Not authenticated' });
});

// GET /api/auth/config - return Microsoft auth configuration for frontend
app.get('/api/auth/config', (req, res) => {
  res.json({
    microsoftAuth: {
      enabled: !!MS_CLIENT_ID,
      clientId: MS_CLIENT_ID,
      tenantId: MS_TENANT_ID,
    },
  });
});

// POST /api/auth/microsoft - authenticate with Microsoft ID token
app.post('/api/auth/microsoft', async (req, res) => {
  if (!MS_CLIENT_ID) {
    return res.status(400).json({ error: 'Microsoft authentication is not configured' });
  }
  const { idToken } = req.body;
  if (!idToken) {
    return res.status(400).json({ error: 'ID token is required' });
  }
  try {
    const decoded = await verifyMicrosoftToken(idToken);
    const microsoftId = decoded.oid || decoded.sub;
    const email = decoded.preferred_username || decoded.email || '';
    const displayName = decoded.name || '';
    const user = findOrCreateMicrosoftUser(microsoftId, email, displayName);
    req.session.userId = user.id;
    req.session.username = user.username;
    res.json({ username: user.username });
  } catch (err) {
    res.status(401).json({ error: 'Invalid Microsoft token' });
  }
});

// --- API key management routes (require session auth) ---

// GET /api/keys - list API keys for current user
app.get('/api/keys', requireAuth, (req, res) => {
  const keys = db.prepare(
    'SELECT id, key_prefix, name, created_at, last_used_at FROM api_keys WHERE user_id = ? ORDER BY created_at DESC'
  ).all(req.session.userId);
  res.json(keys);
});

// POST /api/keys - create a new API key
app.post('/api/keys', requireAuth, (req, res) => {
  const { name } = req.body;
  if (!name || typeof name !== 'string' || !name.trim()) {
    return res.status(400).json({ error: 'Key name is required' });
  }
  if (name.trim().length > 100) {
    return res.status(400).json({ error: 'Key name must be 100 characters or fewer' });
  }
  // Generate a random API key
  const rawKey = 'td_' + crypto.randomBytes(32).toString('hex');
  const keyHash = crypto.createHash('sha256').update(rawKey).digest('hex');
  const keyPrefix = rawKey.slice(0, 10) + '...';
  const result = db.prepare(
    'INSERT INTO api_keys (key_hash, key_prefix, name, user_id) VALUES (?, ?, ?, ?)'
  ).run(keyHash, keyPrefix, sanitize(name.trim()), req.session.userId);
  // Return the full key ONLY on creation
  res.status(201).json({
    id: result.lastInsertRowid,
    key: rawKey,
    key_prefix: keyPrefix,
    name: sanitize(name.trim()),
  });
});

// DELETE /api/keys/:id - revoke an API key
app.delete('/api/keys/:id', requireAuth, (req, res) => {
  const result = db.prepare('DELETE FROM api_keys WHERE id = ? AND user_id = ?').run(req.params.id, req.session.userId);
  if (result.changes === 0) {
    return res.status(404).json({ error: 'API key not found' });
  }
  res.status(204).end();
});

// --- Health endpoint ---
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// --- Protected API routes (session or API key auth) ---

// GET /api/todos - list user's todos
app.get('/api/todos', requireAnyAuth, (req, res) => {
  const userId = req.authUserId;
  const { status, priority, tag, due, search } = req.query;

  let query = 'SELECT id, text, done, sort_order, priority, due_date FROM todos WHERE user_id = ?';
  const params = [userId];

  if (status === 'active') { query += ' AND done = 0'; }
  else if (status === 'done' || status === 'completed') { query += ' AND done = 1'; }

  if (['high', 'medium', 'low'].includes(priority)) {
    query += ' AND priority = ?';
    params.push(priority);
  }

  if (due === 'overdue') {
    query += " AND done = 0 AND due_date IS NOT NULL AND due_date < date('now')";
  } else if (due === 'today') {
    query += " AND due_date = date('now')";
  } else if (due === 'upcoming') {
    query += " AND due_date IS NOT NULL AND due_date >= date('now')";
  } else if (due === 'no-date') {
    query += ' AND due_date IS NULL';
  }

  query += ' ORDER BY sort_order DESC, id DESC';
  const rows = db.prepare(query).all(...params);

  let todos = rows.map(r => ({
    id: r.id, text: r.text, done: !!r.done, priority: r.priority,
    due_date: r.due_date || null, tags: getTagsForTodo(r.id)
  }));

  // Filter by tag (needs post-query filtering since tags are in a junction table)
  if (tag) {
    todos = todos.filter(t => t.tags.some(tg => tg.name === tag));
  }

  // Search filter
  if (search) {
    const q = search.toLowerCase();
    todos = todos.filter(t =>
      t.text.toLowerCase().includes(q) ||
      t.tags.some(tg => tg.name.includes(q))
    );
  }

  res.json(todos);
});

// GET /api/tags - list tags for user's todos
app.get('/api/tags', requireAnyAuth, (req, res) => {
  const userId = req.authUserId;
  const tags = db.prepare(`
    SELECT t.id, t.name, COUNT(tt.todo_id) AS count
    FROM tags t
    JOIN todo_tags tt ON tt.tag_id = t.id
    JOIN todos td ON td.id = tt.todo_id AND td.user_id = ?
    GROUP BY t.id ORDER BY count DESC, t.name ASC
  `).all(userId);
  res.json(tags);
});

// POST /api/todos - create a new todo
app.post('/api/todos', requireAnyAuth, (req, res) => {
  const userId = req.authUserId;
  const { text, priority, due_date, tags } = req.body;
  if (!text || typeof text !== 'string' || !text.trim()) {
    return res.status(400).json({ error: 'Text is required' });
  }
  if (text.trim().length > MAX_TEXT_LENGTH) {
    return res.status(400).json({ error: `Text must be ${MAX_TEXT_LENGTH} characters or fewer` });
  }
  const sanitizedText = sanitize(text.trim());
  const validPriorities = ['high', 'medium', 'low'];
  const todoPriority = validPriorities.includes(priority) ? priority : 'medium';
  const todoDueDate = due_date && typeof due_date === 'string' ? due_date : null;
  const maxOrder = db.prepare('SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM todos WHERE user_id = ?').get(userId);
  const nextOrder = maxOrder.max_order + 1;
  const result = db.prepare('INSERT INTO todos (text, done, sort_order, priority, due_date, user_id) VALUES (?, 0, ?, ?, ?, ?)').run(sanitizedText, nextOrder, todoPriority, todoDueDate, userId);
  const todoId = result.lastInsertRowid;
  if (Array.isArray(tags)) syncTags(todoId, tags);
  res.status(201).json({ id: todoId, text: sanitizedText, done: false, priority: todoPriority, due_date: todoDueDate, tags: getTagsForTodo(todoId) });
});

// PUT /api/todos/:id - update a todo
app.put('/api/todos/:id', requireAnyAuth, (req, res) => {
  const { id } = req.params;
  const userId = req.authUserId;
  const existing = db.prepare('SELECT id, text, done, priority, due_date FROM todos WHERE id = ? AND user_id = ?').get(id, userId);
  if (!existing) {
    return res.status(404).json({ error: 'Todo not found' });
  }

  const text = req.body.text !== undefined ? req.body.text : existing.text;
  const done = req.body.done !== undefined ? (req.body.done ? 1 : 0) : existing.done;
  const validPriorities = ['high', 'medium', 'low'];
  const priority = req.body.priority !== undefined && validPriorities.includes(req.body.priority)
    ? req.body.priority : existing.priority;
  const due_date = req.body.due_date !== undefined
    ? (req.body.due_date && typeof req.body.due_date === 'string' ? req.body.due_date : null)
    : existing.due_date;

  if (typeof text !== 'string' || !text.trim()) {
    return res.status(400).json({ error: 'Text cannot be empty' });
  }
  if (text.trim().length > MAX_TEXT_LENGTH) {
    return res.status(400).json({ error: `Text must be ${MAX_TEXT_LENGTH} characters or fewer` });
  }
  const sanitizedText = sanitize(text.trim());

  db.prepare('UPDATE todos SET text = ?, done = ?, priority = ?, due_date = ? WHERE id = ? AND user_id = ?').run(sanitizedText, done, priority, due_date, id, userId);
  if (req.body.tags !== undefined && Array.isArray(req.body.tags)) syncTags(Number(id), req.body.tags);
  res.json({ id: Number(id), text: sanitizedText, done: !!done, priority, due_date: due_date || null, tags: getTagsForTodo(Number(id)) });
});

// DELETE /api/todos/:id - delete a todo
app.delete('/api/todos/:id', requireAnyAuth, (req, res) => {
  const { id } = req.params;
  const userId = req.authUserId;
  const result = db.prepare('DELETE FROM todos WHERE id = ? AND user_id = ?').run(id, userId);
  if (result.changes === 0) {
    return res.status(404).json({ error: 'Todo not found' });
  }
  res.status(204).end();
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Todo server running on port ${PORT}`);
});
