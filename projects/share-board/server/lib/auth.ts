import type { Request, Response, NextFunction } from "express";
import db from "../db/index.js";

export function requireEditToken(
  req: Request,
  res: Response,
  next: NextFunction,
): void {
  const editToken = req.headers["x-edit-token"] as string | undefined;
  const boardId = req.params.id;

  if (!editToken) {
    res.status(401).json({ error: "Missing edit token" });
    return;
  }

  const board = db
    .prepare("SELECT id FROM boards WHERE id = ? AND edit_token = ?")
    .get(boardId, editToken) as { id: string } | undefined;

  if (!board) {
    res.status(403).json({ error: "Invalid edit token" });
    return;
  }

  next();
}
