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
    priority TEXT NOT NULL DEFAULT 'medium',
    due_date TEXT DEFAULT NULL
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
app.use(express.static(path.join(__dirname, 'public')));

// GET /api/todos - list all todos ordered by sort_order desc (newest first)
app.get('/api/todos', (req, res) => {
  const rows = db.prepare('SELECT id, text, done, sort_order, priority, due_date FROM todos ORDER BY sort_order DESC, id DESC').all();
  const todos = rows.map(r => ({
    id: r.id, text: r.text, done: !!r.done, priority: r.priority,
    due_date: r.due_date || null, tags: getTagsForTodo(r.id)
  }));
  res.json(todos);
});

// GET /api/tags - list all tags with usage counts
app.get('/api/tags', (req, res) => {
  const tags = db.prepare(`
    SELECT t.id, t.name, COUNT(tt.todo_id) AS count
    FROM tags t LEFT JOIN todo_tags tt ON tt.tag_id = t.id
    GROUP BY t.id ORDER BY count DESC, t.name ASC
  `).all();
  res.json(tags);
});

// POST /api/todos - create a new todo
app.post('/api/todos', (req, res) => {
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
  const maxOrder = db.prepare('SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM todos').get();
  const nextOrder = maxOrder.max_order + 1;
  const result = db.prepare('INSERT INTO todos (text, done, sort_order, priority, due_date) VALUES (?, 0, ?, ?, ?)').run(sanitizedText, nextOrder, todoPriority, todoDueDate);
  const todoId = result.lastInsertRowid;
  if (Array.isArray(tags)) syncTags(todoId, tags);
  res.status(201).json({ id: todoId, text: sanitizedText, done: false, priority: todoPriority, due_date: todoDueDate, tags: getTagsForTodo(todoId) });
});

// PUT /api/todos/:id - update a todo (text and/or done status)
app.put('/api/todos/:id', (req, res) => {
  const { id } = req.params;
  const existing = db.prepare('SELECT id, text, done, priority, due_date FROM todos WHERE id = ?').get(id);
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

  db.prepare('UPDATE todos SET text = ?, done = ?, priority = ?, due_date = ? WHERE id = ?').run(sanitizedText, done, priority, due_date, id);
  if (req.body.tags !== undefined && Array.isArray(req.body.tags)) syncTags(Number(id), req.body.tags);
  res.json({ id: Number(id), text: sanitizedText, done: !!done, priority, due_date: due_date || null, tags: getTagsForTodo(Number(id)) });
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
