import { WebSocketServer, WebSocket } from "ws";
import type { Server } from "node:http";
import { URL } from "node:url";
import db from "../db/index.js";

interface BoardRoom {
  creator: WebSocket | null;
  viewers: Set<WebSocket>;
  editToken: string | null;
}

const rooms = new Map<string, BoardRoom>();

function getOrCreateRoom(boardId: string): BoardRoom {
  let room = rooms.get(boardId);
  if (!room) {
    room = { creator: null, viewers: new Set(), editToken: null };
    rooms.set(boardId, room);
  }
  return room;
}

function cleanupRoom(boardId: string) {
  const room = rooms.get(boardId);
  if (room && !room.creator && room.viewers.size === 0) {
    rooms.delete(boardId);
  }
}

export function setupWebSocket(server: Server) {
  const wss = new WebSocketServer({ noServer: true });

  server.on("upgrade", (request, socket, head) => {
    const url = new URL(request.url || "/", `http://${request.headers.host}`);
    if (url.pathname !== "/ws") {
      socket.destroy();
      return;
    }

    wss.handleUpgrade(request, socket, head, (ws) => {
      wss.emit("connection", ws, request);
    });
  });

  wss.on("connection", (ws, request) => {
    const url = new URL(request.url || "/", `http://${request.headers.host}`);
    const boardId = url.searchParams.get("boardId");

    if (!boardId) {
      ws.close(1008, "Missing boardId");
      return;
    }

    // Check board exists
    const board = db
      .prepare("SELECT id, snapshot FROM boards WHERE id = ?")
      .get(boardId) as { id: string; snapshot: string } | undefined;

    if (!board) {
      ws.close(1008, "Board not found");
      return;
    }

    const room = getOrCreateRoom(boardId);
    room.viewers.add(ws);

    // Send current snapshot to newly connected client
    ws.send(JSON.stringify({ type: "snapshot", data: board.snapshot }));

    ws.on("message", (raw) => {
      try {
        const msg = JSON.parse(raw.toString());

        if (msg.type === "update" && msg.editToken && msg.data) {
          // Verify edit token
          const valid = db
            .prepare(
              "SELECT id FROM boards WHERE id = ? AND edit_token = ?",
            )
            .get(boardId, msg.editToken) as { id: string } | undefined;

          if (!valid) return;

          // Mark this ws as creator
          room.creator = ws;
          room.editToken = msg.editToken;

          // Broadcast to all other viewers
          const payload = JSON.stringify({
            type: "update",
            data: msg.data,
          });

          for (const viewer of room.viewers) {
            if (viewer !== ws && viewer.readyState === WebSocket.OPEN) {
              viewer.send(payload);
            }
          }
        }

        if (msg.type === "laser" && msg.editToken && msg.data) {
          // Verify edit token for laser pointer
          const valid = db
            .prepare(
              "SELECT id FROM boards WHERE id = ? AND edit_token = ?",
            )
            .get(boardId, msg.editToken) as { id: string } | undefined;

          if (!valid) return;

          room.creator = ws;
          room.editToken = msg.editToken;

          // Broadcast laser pointer data to all viewers
          const payload = JSON.stringify({
            type: "laser",
            data: msg.data,
          });

          for (const viewer of room.viewers) {
            if (viewer !== ws && viewer.readyState === WebSocket.OPEN) {
              viewer.send(payload);
            }
          }
        }
      } catch {
        // Ignore malformed messages
      }
    });

    ws.on("close", () => {
      room.viewers.delete(ws);
      if (room.creator === ws) {
        room.creator = null;
        room.editToken = null;
      }
      cleanupRoom(boardId);
    });
  });
}
