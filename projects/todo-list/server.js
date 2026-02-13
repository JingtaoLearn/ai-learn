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
    tags TEXT NOT NULL DEFAULT '[]'
  )
`);

// Migration: add tags column if it doesn't exist
const columns = db.prepare("PRAGMA table_info(todos)").all();
if (!columns.find(c => c.name === 'tags')) {
  db.exec("ALTER TABLE todos ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'");
}

// Middleware
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// GET /api/todos - list all todos ordered by sort_order desc (newest first)
// Optional query param: ?tag=work to filter by tag
app.get('/api/todos', (req, res) => {
  const rows = db.prepare('SELECT id, text, done, sort_order, tags FROM todos ORDER BY sort_order DESC, id DESC').all();
  let todos = rows.map(r => ({
    id: r.id,
    text: r.text,
    done: !!r.done,
    tags: JSON.parse(r.tags || '[]'),
  }));
  const { tag } = req.query;
  if (tag) {
    todos = todos.filter(t => t.tags.includes(tag));
  }
  res.json(todos);
});

// GET /api/tags - list all unique tags with counts
app.get('/api/tags', (req, res) => {
  const rows = db.prepare('SELECT tags FROM todos').all();
  const tagCounts = {};
  for (const row of rows) {
    const tags = JSON.parse(row.tags || '[]');
    for (const tag of tags) {
      tagCounts[tag] = (tagCounts[tag] || 0) + 1;
    }
  }
  const result = Object.entries(tagCounts)
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count);
  res.json(result);
});

// POST /api/todos - create a new todo
app.post('/api/todos', (req, res) => {
  const { text, tags } = req.body;
  if (!text || typeof text !== 'string' || !text.trim()) {
    return res.status(400).json({ error: 'Text is required' });
  }
  const parsedTags = Array.isArray(tags)
    ? tags.map(t => String(t).trim().toLowerCase()).filter(Boolean)
    : [];
  const maxOrder = db.prepare('SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM todos').get();
  const nextOrder = maxOrder.max_order + 1;
  const result = db.prepare('INSERT INTO todos (text, done, sort_order, tags) VALUES (?, 0, ?, ?)').run(text.trim(), nextOrder, JSON.stringify(parsedTags));
  res.status(201).json({ id: result.lastInsertRowid, text: text.trim(), done: false, tags: parsedTags });
});

// PUT /api/todos/:id - update a todo (text, done status, and/or tags)
app.put('/api/todos/:id', (req, res) => {
  const { id } = req.params;
  const existing = db.prepare('SELECT id, text, done, tags FROM todos WHERE id = ?').get(id);
  if (!existing) {
    return res.status(404).json({ error: 'Todo not found' });
  }

  const text = req.body.text !== undefined ? req.body.text : existing.text;
  const done = req.body.done !== undefined ? (req.body.done ? 1 : 0) : existing.done;
  const tags = req.body.tags !== undefined
    ? (Array.isArray(req.body.tags) ? req.body.tags.map(t => String(t).trim().toLowerCase()).filter(Boolean) : [])
    : JSON.parse(existing.tags || '[]');

  if (typeof text !== 'string' || !text.trim()) {
    return res.status(400).json({ error: 'Text cannot be empty' });
  }

  db.prepare('UPDATE todos SET text = ?, done = ?, tags = ? WHERE id = ?').run(text.trim(), done, JSON.stringify(tags), id);
  res.json({ id: Number(id), text: text.trim(), done: !!done, tags });
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
