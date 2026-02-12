import { Router } from "express";
import { nanoid } from "nanoid";
import db from "../db/index.js";
import { requireEditToken } from "../lib/auth.js";

const router = Router();

const BASE_URL = process.env.BASE_URL || "http://localhost:5173";

router.post("/", (req, res) => {
  const { snapshot } = req.body;
  if (!snapshot) {
    res.status(400).json({ error: "Missing snapshot" });
    return;
  }

  const id = nanoid(10);
  const editToken = nanoid(24);
  const snapshotStr =
    typeof snapshot === "string" ? snapshot : JSON.stringify(snapshot);

  db.prepare(
    "INSERT INTO boards (id, edit_token, snapshot) VALUES (?, ?, ?)",
  ).run(id, editToken, snapshotStr);

  res.json({
    id,
    editToken,
    shareUrl: `${BASE_URL}/view/${id}`,
  });
});

router.get("/:id", (req, res) => {
  const board = db
    .prepare("SELECT id, snapshot, updated_at FROM boards WHERE id = ?")
    .get(req.params.id) as
    | { id: string; snapshot: string; updated_at: string }
    | undefined;

  if (!board) {
    res.status(404).json({ error: "Board not found" });
    return;
  }

  res.json({
    id: board.id,
    snapshot: JSON.parse(board.snapshot),
    updatedAt: board.updated_at,
  });
});

router.put("/:id", requireEditToken, (req, res) => {
  const { snapshot } = req.body;
  if (!snapshot) {
    res.status(400).json({ error: "Missing snapshot" });
    return;
  }

  const snapshotStr =
    typeof snapshot === "string" ? snapshot : JSON.stringify(snapshot);

  db.prepare(
    "UPDATE boards SET snapshot = ?, updated_at = datetime('now') WHERE id = ?",
  ).run(snapshotStr, req.params.id);

  res.json({ ok: true });
});

export default router;
