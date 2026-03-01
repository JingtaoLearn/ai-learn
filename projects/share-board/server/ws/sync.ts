import { WebSocketServer, WebSocket } from "ws";
import type { Server } from "node:http";
import { URL } from "node:url";
import { nanoid } from "nanoid";
import db from "../db/index.js";

interface BoardRoom {
  creator: WebSocket | null;
  creatorViewerId: string | null;
  viewers: Map<string, WebSocket>;
  editToken: string | null;
  grantedEditors: Set<string>;
}

const rooms = new Map<string, BoardRoom>();

function getOrCreateRoom(boardId: string): BoardRoom {
  let room = rooms.get(boardId);
  if (!room) {
    room = {
      creator: null,
      creatorViewerId: null,
      viewers: new Map(),
      editToken: null,
      grantedEditors: new Set(),
    };
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
    const existingViewerId = url.searchParams.get("viewerId");
    const viewerId = existingViewerId && room.grantedEditors.has(existingViewerId)
      ? existingViewerId
      : nanoid(12);

    // Clean up stale WebSocket if reconnecting with same viewerId
    if (existingViewerId && existingViewerId === viewerId) {
      const oldWs = room.viewers.get(viewerId);
      if (oldWs && oldWs !== ws && oldWs.readyState !== WebSocket.CLOSED) {
        oldWs.close();
      }
    }

    room.viewers.set(viewerId, ws);
    const isReconnectedEditor = room.grantedEditors.has(viewerId);

    // Send current snapshot and viewer ID to newly connected client
    ws.send(JSON.stringify({ type: "snapshot", data: board.snapshot, isReconnect: isReconnectedEditor }));
    ws.send(JSON.stringify({ type: "viewer-id", viewerId }));

    // Re-grant edit access if this was a granted editor reconnecting
    if (isReconnectedEditor && room.editToken) {
      ws.send(JSON.stringify({ type: "edit-granted", editToken: room.editToken }));
    }

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

          // Only set creator if this is the original owner (not a granted editor)
          const isGrantedEditor = room.grantedEditors.has(viewerId);
          if (!isGrantedEditor) {
            room.creator = ws;
            room.creatorViewerId = viewerId;
          }
          room.editToken = msg.editToken;

          // Broadcast to all other viewers
          const payload = JSON.stringify({
            type: "update",
            data: msg.data,
          });

          for (const [, viewer] of room.viewers) {
            if (viewer !== ws && viewer.readyState === WebSocket.OPEN) {
              viewer.send(payload);
            }
          }
        }

        // Laser pointer
        if (msg.type === "laser" && msg.editToken && msg.data) {
          // Verify edit token for laser pointer
          const valid = db
            .prepare(
              "SELECT id FROM boards WHERE id = ? AND edit_token = ?",
            )
            .get(boardId, msg.editToken) as { id: string } | undefined;

          if (!valid) return;

          // Only set creator if this is the original owner (not a granted editor)
          const isGrantedEditor = room.grantedEditors.has(viewerId);
          if (!isGrantedEditor) {
            room.creator = ws;
            room.creatorViewerId = viewerId;
          }
          room.editToken = msg.editToken;

          // Broadcast laser pointer data to all viewers
          const payload = JSON.stringify({
            type: "laser",
            data: msg.data,
          });

          for (const [, viewer] of room.viewers) {
            if (viewer !== ws && viewer.readyState === WebSocket.OPEN) {
              viewer.send(payload);
            }
          }
        }

        // Viewer requests edit access
        if (msg.type === "edit-request") {
          if (room.creator && room.creator.readyState === WebSocket.OPEN) {
            room.creator.send(
              JSON.stringify({
                type: "edit-request",
                viewerId,
              }),
            );
          } else {
            // Owner not online, deny
            ws.send(JSON.stringify({ type: "edit-denied" }));
          }
        }

        // Owner responds to edit request
        if (msg.type === "edit-response" && msg.editToken && msg.viewerId !== undefined) {
          // Verify this is the owner
          const valid = db
            .prepare(
              "SELECT id FROM boards WHERE id = ? AND edit_token = ?",
            )
            .get(boardId, msg.editToken) as { id: string } | undefined;

          if (!valid) return;

          const targetWs = room.viewers.get(msg.viewerId);
          if (!targetWs || targetWs.readyState !== WebSocket.OPEN) return;

          if (msg.approved) {
            room.grantedEditors.add(msg.viewerId);
            targetWs.send(
              JSON.stringify({
                type: "edit-granted",
                editToken: room.editToken,
              }),
            );
          } else {
            targetWs.send(JSON.stringify({ type: "edit-denied" }));
          }
        }
      } catch {
        // Ignore malformed messages
      }
    });

    ws.on("close", () => {
      room.viewers.delete(viewerId);
      room.grantedEditors.delete(viewerId);

      if (room.creatorViewerId === viewerId) {
        // Owner disconnected — clear creator reference but keep granted editors
        // so they can continue editing. Owner reconnecting will restore the reference.
        room.creator = null;
        room.creatorViewerId = null;
      }
      cleanupRoom(boardId);
    });
  });
}
