const express = require('express');
const Database = require('better-sqlite3');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 80;
const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'data', 'notes.db');

// Ensure data directory exists
const fs = require('fs');
const dbDir = path.dirname(DB_PATH);
if (!fs.existsSync(dbDir)) {
  fs.mkdirSync(dbDir, { recursive: true });
}

const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

// Create tables
db.exec(`
  CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
  );

  CREATE TABLE IF NOT EXISTS note_tags (
    note_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (note_id, tag_id),
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
  );
`);

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Sanitize HTML to prevent XSS
function escapeHtml(str) {
  if (typeof str !== 'string') return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// GET /api/notes — list all notes with tags
app.get('/api/notes', (req, res) => {
  const notes = db.prepare(`
    SELECT id, title, content, pinned, created_at, updated_at
    FROM notes
    ORDER BY pinned DESC, updated_at DESC
  `).all();

  const tagStmt = db.prepare(`
    SELECT t.id, t.name
    FROM tags t
    JOIN note_tags nt ON nt.tag_id = t.id
    WHERE nt.note_id = ?
    ORDER BY t.name
  `);

  const result = notes.map(note => ({
    ...note,
    tags: tagStmt.all(note.id)
  }));

  res.json(result);
});

// POST /api/notes — create a note
app.post('/api/notes', (req, res) => {
  const title = (req.body.title || '').trim();
  const content = (req.body.content || '').trim();
  const tags = req.body.tags || [];

  // Allow empty notes — user creates first, then types content

  if (title.length > 200) {
    return res.status(400).json({ error: 'Title must be 200 characters or less' });
  }

  if (content.length > 50000) {
    return res.status(400).json({ error: 'Content must be 50000 characters or less' });
  }

  const sanitizedTitle = escapeHtml(title);

  const insertNote = db.prepare(`
    INSERT INTO notes (title, content) VALUES (?, ?)
  `);

  const result = insertNote.run(sanitizedTitle, content);
  const noteId = result.lastInsertRowid;

  // Add tags
  if (Array.isArray(tags) && tags.length > 0) {
    const insertTag = db.prepare(`INSERT OR IGNORE INTO tags (name) VALUES (?)`);
    const getTagId = db.prepare(`SELECT id FROM tags WHERE name = ?`);
    const linkTag = db.prepare(`INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)`);

    for (const tagName of tags) {
      const cleaned = tagName.trim().toLowerCase().slice(0, 50);
      if (!cleaned) continue;
      insertTag.run(cleaned);
      const tag = getTagId.get(cleaned);
      if (tag) linkTag.run(noteId, tag.id);
    }
  }

  const note = db.prepare(`SELECT * FROM notes WHERE id = ?`).get(noteId);
  const noteTags = db.prepare(`
    SELECT t.id, t.name FROM tags t
    JOIN note_tags nt ON nt.tag_id = t.id
    WHERE nt.note_id = ?
  `).all(noteId);

  res.status(201).json({ ...note, tags: noteTags });
});

// PUT /api/notes/:id — update a note
app.put('/api/notes/:id', (req, res) => {
  const { id } = req.params;
  const existing = db.prepare(`SELECT * FROM notes WHERE id = ?`).get(id);

  if (!existing) {
    return res.status(404).json({ error: 'Note not found' });
  }

  const updates = {};

  if (req.body.title !== undefined) {
    const title = req.body.title.trim();
    if (title.length > 200) {
      return res.status(400).json({ error: 'Title must be 200 characters or less' });
    }
    updates.title = escapeHtml(title);
  }

  if (req.body.content !== undefined) {
    const content = req.body.content.trim();
    if (content.length > 50000) {
      return res.status(400).json({ error: 'Content must be 50000 characters or less' });
    }
    updates.content = content;
  }

  if (req.body.pinned !== undefined) {
    updates.pinned = req.body.pinned ? 1 : 0;
  }

  const setClauses = Object.keys(updates).map(k => `${k} = ?`);
  setClauses.push(`updated_at = datetime('now')`);

  const stmt = db.prepare(`
    UPDATE notes SET ${setClauses.join(', ')} WHERE id = ?
  `);
  stmt.run(...Object.values(updates), id);

  // Handle tags if provided
  if (req.body.tags !== undefined && Array.isArray(req.body.tags)) {
    db.prepare(`DELETE FROM note_tags WHERE note_id = ?`).run(id);

    const insertTag = db.prepare(`INSERT OR IGNORE INTO tags (name) VALUES (?)`);
    const getTagId = db.prepare(`SELECT id FROM tags WHERE name = ?`);
    const linkTag = db.prepare(`INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)`);

    for (const tagName of req.body.tags) {
      const cleaned = tagName.trim().toLowerCase().slice(0, 50);
      if (!cleaned) continue;
      insertTag.run(cleaned);
      const tag = getTagId.get(cleaned);
      if (tag) linkTag.run(id, tag.id);
    }
  }

  const note = db.prepare(`SELECT * FROM notes WHERE id = ?`).get(id);
  const tags = db.prepare(`
    SELECT t.id, t.name FROM tags t
    JOIN note_tags nt ON nt.tag_id = t.id
    WHERE nt.note_id = ?
  `).all(id);

  res.json({ ...note, tags });
});

// DELETE /api/notes/:id — delete a note
app.delete('/api/notes/:id', (req, res) => {
  const { id } = req.params;
  const existing = db.prepare(`SELECT * FROM notes WHERE id = ?`).get(id);

  if (!existing) {
    return res.status(404).json({ error: 'Note not found' });
  }

  db.prepare(`DELETE FROM notes WHERE id = ?`).run(id);

  // Clean up orphaned tags
  db.prepare(`
    DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM note_tags)
  `).run();

  res.json({ success: true });
});

// GET /api/tags — list all tags with counts
app.get('/api/tags', (req, res) => {
  const tags = db.prepare(`
    SELECT t.id, t.name, COUNT(nt.note_id) as count
    FROM tags t
    LEFT JOIN note_tags nt ON nt.tag_id = t.id
    GROUP BY t.id
    ORDER BY t.name
  `).all();

  res.json(tags);
});

// POST /api/notes/:id/tags — add a tag to a note
app.post('/api/notes/:id/tags', (req, res) => {
  const { id } = req.params;
  const existing = db.prepare(`SELECT * FROM notes WHERE id = ?`).get(id);

  if (!existing) {
    return res.status(404).json({ error: 'Note not found' });
  }

  const name = (req.body.name || '').trim().toLowerCase().slice(0, 50);
  if (!name) {
    return res.status(400).json({ error: 'Tag name is required' });
  }

  db.prepare(`INSERT OR IGNORE INTO tags (name) VALUES (?)`).run(name);
  const tag = db.prepare(`SELECT id, name FROM tags WHERE name = ?`).get(name);
  db.prepare(`INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)`).run(id, tag.id);

  // Update note's updated_at
  db.prepare(`UPDATE notes SET updated_at = datetime('now') WHERE id = ?`).run(id);

  res.status(201).json(tag);
});

// DELETE /api/notes/:id/tags/:tagId — remove a tag from a note
app.delete('/api/notes/:id/tags/:tagId', (req, res) => {
  const { id, tagId } = req.params;

  db.prepare(`DELETE FROM note_tags WHERE note_id = ? AND tag_id = ?`).run(id, tagId);

  // Clean up orphaned tags
  db.prepare(`
    DELETE FROM tags WHERE id = ? AND id NOT IN (SELECT DISTINCT tag_id FROM note_tags)
  `).run(tagId);

  // Update note's updated_at
  db.prepare(`UPDATE notes SET updated_at = datetime('now') WHERE id = ?`).run(id);

  res.json({ success: true });
});

app.listen(PORT, () => {
  console.log(`Note app listening on port ${PORT}`);
});
