import express from "express";
import cors from "cors";
import path from "node:path";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import boardsRouter from "./routes/boards.js";
import assetsRouter from "./routes/assets.js";
import { setupWebSocket } from "./ws/sync.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const server = createServer(app);

app.use(cors());
app.use(express.json({ limit: "50mb" }));

// API routes
app.use("/api/boards", boardsRouter);
app.use("/api", assetsRouter);

// Serve static frontend in production
const clientDir = path.join(__dirname, "../client");
app.use(express.static(clientDir));

// SPA fallback - serve index.html for all non-API routes
app.get("*", (_req, res) => {
  res.sendFile(path.join(clientDir, "index.html"));
});

// WebSocket setup
setupWebSocket(server);

const PORT = Number(process.env.PORT) || 3000;
server.listen(PORT, () => {
  console.log(`Share Board server running on port ${PORT}`);
});
