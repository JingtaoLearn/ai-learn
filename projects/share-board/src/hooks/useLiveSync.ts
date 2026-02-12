import { useEffect, useRef } from "react";
import { BoardWebSocket } from "../lib/ws";

export function useLiveSync(
  boardId: string | null,
  onSnapshot: (data: string) => void,
  onUpdate: (data: string) => void,
) {
  const wsRef = useRef<BoardWebSocket | null>(null);

  useEffect(() => {
    if (!boardId) return;

    wsRef.current = new BoardWebSocket(boardId, (msg) => {
      if (msg.type === "snapshot") {
        onSnapshot(msg.data);
      } else if (msg.type === "update") {
        onUpdate(msg.data);
      }
    });

    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [boardId, onSnapshot, onUpdate]);

  return wsRef;
}
