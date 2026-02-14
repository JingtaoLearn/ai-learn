const express = require('express');
const Database = require('better-sqlite3');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 80;
const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'data', 'todos.db');

// Ensure data directory exists
const fs = require('fs');
fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });

// Initialize SQLite database
const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.exec(`
  CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    priority TEXT NOT NULL DEFAULT 'medium'
  )
`);

// Migration: add priority column if it doesn't exist
try {
  db.exec(`ALTER TABLE todos ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'`);
} catch (e) {
  // Column already exists, ignore
}

// Validation constants
const MAX_TODO_LENGTH = 500;

// Sanitize string to prevent XSS (strip HTML tags)
function sanitize(str) {
  return str.replace(/[<>&"']/g, ch => ({
    '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;'
  })[ch]);
}

// Validate todo text: must be a non-empty string within length limit
function validateTodoText(text) {
  if (!text || typeof text !== 'string' || !text.trim()) {
    return { valid: false, error: 'Text is required' };
  }
  if (text.trim().length > MAX_TODO_LENGTH) {
    return { valid: false, error: `Text must be ${MAX_TODO_LENGTH} characters or fewer` };
  }
  return { valid: true, text: sanitize(text.trim()) };
}

// Middleware
app.use(express.json({ limit: '10kb' }));
app.use(express.static(path.join(__dirname, 'public')));

// GET /api/todos - list all todos ordered by sort_order desc (newest first)
app.get('/api/todos', (req, res) => {
  const rows = db.prepare('SELECT id, text, done, sort_order, priority FROM todos ORDER BY sort_order DESC, id DESC').all();
  const todos = rows.map(r => ({ id: r.id, text: r.text, done: !!r.done, priority: r.priority }));
  res.json(todos);
});

// POST /api/todos - create a new todo
app.post('/api/todos', (req, res) => {
  const { priority } = req.body;
  const validation = validateTodoText(req.body.text);
  if (!validation.valid) {
    return res.status(400).json({ error: validation.error });
  }
  const sanitizedText = validation.text;
  const validPriorities = ['high', 'medium', 'low'];
  const todoPriority = validPriorities.includes(priority) ? priority : 'medium';
  const maxOrder = db.prepare('SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM todos').get();
  const nextOrder = maxOrder.max_order + 1;
  const result = db.prepare('INSERT INTO todos (text, done, sort_order, priority) VALUES (?, 0, ?, ?)').run(sanitizedText, nextOrder, todoPriority);
  res.status(201).json({ id: result.lastInsertRowid, text: sanitizedText, done: false, priority: todoPriority });
});

// PUT /api/todos/:id - update a todo (text and/or done status)
app.put('/api/todos/:id', (req, res) => {
  const { id } = req.params;
  const existing = db.prepare('SELECT id, text, done, priority FROM todos WHERE id = ?').get(id);
  if (!existing) {
    return res.status(404).json({ error: 'Todo not found' });
  }

  let text = existing.text;
  if (req.body.text !== undefined) {
    const validation = validateTodoText(req.body.text);
    if (!validation.valid) {
      return res.status(400).json({ error: validation.error });
    }
    text = validation.text;
  }

  const done = req.body.done !== undefined ? (req.body.done ? 1 : 0) : existing.done;
  const validPriorities = ['high', 'medium', 'low'];
  const priority = req.body.priority !== undefined && validPriorities.includes(req.body.priority)
    ? req.body.priority : existing.priority;

  db.prepare('UPDATE todos SET text = ?, done = ?, priority = ? WHERE id = ?').run(text, done, priority, id);
  res.json({ id: Number(id), text, done: !!done, priority });
});

// DELETE /api/todos/:id - delete a todo
app.delete('/api/todos/:id', (req, res) => {
  const { id } = req.params;
  const result = db.prepare('DELETE FROM todos WHERE id = ?').run(id);
  if (result.changes === 0) {
    return res.status(404).json({ error: 'Todo not found' });
  }
  res.status(204).end();
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Todo server running on port ${PORT}`);
});
