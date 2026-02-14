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

// Migration: add tags column if it doesn't exist
try {
  db.exec(`ALTER TABLE todos ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'`);
} catch (e) {
  // Column already exists, ignore
}

// Middleware
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// GET /api/tags - list all unique tags used across todos
app.get('/api/tags', (req, res) => {
  const rows = db.prepare('SELECT tags FROM todos').all();
  const tagSet = new Set();
  rows.forEach(r => {
    try {
      const tags = JSON.parse(r.tags);
      tags.forEach(tag => tagSet.add(tag));
    } catch (e) { /* ignore malformed */ }
  });
  res.json([...tagSet].sort());
});

// GET /api/todos - list all todos ordered by sort_order desc (newest first)
app.get('/api/todos', (req, res) => {
  const rows = db.prepare('SELECT id, text, done, sort_order, priority, tags FROM todos ORDER BY sort_order DESC, id DESC').all();
  const todos = rows.map(r => ({
    id: r.id,
    text: r.text,
    done: !!r.done,
    priority: r.priority,
    tags: (() => { try { return JSON.parse(r.tags); } catch (e) { return []; } })()
  }));
  res.json(todos);
});

// POST /api/todos - create a new todo
app.post('/api/todos', (req, res) => {
  const { text, priority, tags } = req.body;
  if (!text || typeof text !== 'string' || !text.trim()) {
    return res.status(400).json({ error: 'Text is required' });
  }
  const validPriorities = ['high', 'medium', 'low'];
  const todoPriority = validPriorities.includes(priority) ? priority : 'medium';
  const todoTags = Array.isArray(tags) ? tags.filter(t => typeof t === 'string' && t.trim()).map(t => t.trim().toLowerCase()) : [];
  const maxOrder = db.prepare('SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM todos').get();
  const nextOrder = maxOrder.max_order + 1;
  const result = db.prepare('INSERT INTO todos (text, done, sort_order, priority, tags) VALUES (?, 0, ?, ?, ?)').run(text.trim(), nextOrder, todoPriority, JSON.stringify(todoTags));
  res.status(201).json({ id: result.lastInsertRowid, text: text.trim(), done: false, priority: todoPriority, tags: todoTags });
});

// PUT /api/todos/:id - update a todo (text and/or done status)
app.put('/api/todos/:id', (req, res) => {
  const { id } = req.params;
  const existing = db.prepare('SELECT id, text, done, priority, tags FROM todos WHERE id = ?').get(id);
  if (!existing) {
    return res.status(404).json({ error: 'Todo not found' });
  }

  const text = req.body.text !== undefined ? req.body.text : existing.text;
  const done = req.body.done !== undefined ? (req.body.done ? 1 : 0) : existing.done;
  const validPriorities = ['high', 'medium', 'low'];
  const priority = req.body.priority !== undefined && validPriorities.includes(req.body.priority)
    ? req.body.priority : existing.priority;
  let tags;
  if (req.body.tags !== undefined) {
    tags = Array.isArray(req.body.tags) ? req.body.tags.filter(t => typeof t === 'string' && t.trim()).map(t => t.trim().toLowerCase()) : [];
  } else {
    try { tags = JSON.parse(existing.tags); } catch (e) { tags = []; }
  }

  if (typeof text !== 'string' || !text.trim()) {
    return res.status(400).json({ error: 'Text cannot be empty' });
  }

  db.prepare('UPDATE todos SET text = ?, done = ?, priority = ?, tags = ? WHERE id = ?').run(text.trim(), done, priority, JSON.stringify(tags), id);
  res.json({ id: Number(id), text: text.trim(), done: !!done, priority, tags });
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
