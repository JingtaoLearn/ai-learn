import Database from "better-sqlite3";
import path from "node:path";
import fs from "node:fs";

const DATA_DIR = process.env.DATA_DIR || path.join(process.cwd(), "data");
const DB_DIR = path.join(DATA_DIR, "db");

fs.mkdirSync(DB_DIR, { recursive: true });

const db = new Database(path.join(DB_DIR, "share-board.db"));

db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

db.exec(`
  CREATE TABLE IF NOT EXISTS boards (
    id          TEXT PRIMARY KEY,
    edit_token  TEXT NOT NULL UNIQUE,
    snapshot    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
  );

  CREATE INDEX IF NOT EXISTS idx_boards_edit_token ON boards(edit_token);
`);

export default db;
