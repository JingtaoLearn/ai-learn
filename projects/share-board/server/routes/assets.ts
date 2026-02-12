import { Router } from "express";
import multer from "multer";
import path from "node:path";
import fs from "node:fs";
import { nanoid } from "nanoid";
import { requireEditToken } from "../lib/auth.js";

const router = Router();

const DATA_DIR = process.env.DATA_DIR || path.join(process.cwd(), "data");
const ASSETS_DIR = path.join(DATA_DIR, "assets");

const ALLOWED_MIMES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/svg+xml",
]);

const upload = multer({
  limits: { fileSize: 10 * 1024 * 1024 },
  fileFilter: (_req, file, cb) => {
    if (ALLOWED_MIMES.has(file.mimetype)) {
      cb(null, true);
    } else {
      cb(new Error("Only image files are allowed"));
    }
  },
});

// Upload asset for a board
router.post(
  "/boards/:id/assets",
  requireEditToken,
  upload.single("file"),
  (req, res) => {
    if (!req.file) {
      res.status(400).json({ error: "No file uploaded" });
      return;
    }

    const boardId = req.params.id as string;
    const ext = path.extname(req.file.originalname) || ".png";
    const filename = `${nanoid(12)}${ext}`;
    const boardAssetsDir = path.join(ASSETS_DIR, boardId);

    fs.mkdirSync(boardAssetsDir, { recursive: true });
    fs.writeFileSync(path.join(boardAssetsDir, filename), req.file.buffer);

    res.json({ url: `/api/assets/${boardId}/${filename}` });
  },
);

// Serve asset
router.get("/assets/:boardId/:filename", (req, res) => {
  const { boardId, filename } = req.params;
  // Prevent directory traversal
  const safeName = path.basename(filename);
  const filePath = path.join(ASSETS_DIR, boardId, safeName);

  if (!fs.existsSync(filePath)) {
    res.status(404).json({ error: "Asset not found" });
    return;
  }

  res.sendFile(filePath);
});

export default router;
