const express = require('express');
const session = require('express-session');
const BetterSqlite3SessionStore = require('better-sqlite3-session-store');
const Database = require('better-sqlite3');
const bcrypt = require('bcryptjs');
const path = require('path');
const crypto = require('crypto');

const app = express();
const PORT = process.env.PORT || 80;
const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'data', 'todos.db');
const SESSION_SECRET = process.env.SESSION_SECRET || crypto.randomBytes(32).toString('hex');

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

// --- Protected API routes ---

// GET /api/todos - list user's todos
app.get('/api/todos', requireAuth, (req, res) => {
  const userId = req.session.userId;
  const rows = db.prepare('SELECT id, text, done, sort_order, priority, due_date FROM todos WHERE user_id = ? ORDER BY sort_order DESC, id DESC').all(userId);
  const todos = rows.map(r => ({
    id: r.id, text: r.text, done: !!r.done, priority: r.priority,
    due_date: r.due_date || null, tags: getTagsForTodo(r.id)
  }));
  res.json(todos);
});

// GET /api/tags - list tags for user's todos
app.get('/api/tags', requireAuth, (req, res) => {
  const userId = req.session.userId;
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
app.post('/api/todos', requireAuth, (req, res) => {
  const userId = req.session.userId;
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
app.put('/api/todos/:id', requireAuth, (req, res) => {
  const { id } = req.params;
  const userId = req.session.userId;
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
app.delete('/api/todos/:id', requireAuth, (req, res) => {
  const { id } = req.params;
  const userId = req.session.userId;
  const result = db.prepare('DELETE FROM todos WHERE id = ? AND user_id = ?').run(id, userId);
  if (result.changes === 0) {
    return res.status(404).json({ error: 'Todo not found' });
  }
  res.status(204).end();
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Todo server running on port ${PORT}`);
});
