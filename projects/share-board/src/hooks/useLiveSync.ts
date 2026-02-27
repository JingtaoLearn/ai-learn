import { useEffect, useRef } from "react";
import { BoardWebSocket } from "../lib/ws";

export function useLiveSync(
  boardId: string | null,
  onSnapshot: (data: string) => void,
  onUpdate: (data: string) => void,
  onEditRequest?: (viewerId: string) => void,
  onEditGranted?: (editToken: string) => void,
  onEditDenied?: () => void,
  onEditRevoked?: () => void,
) {
  const wsRef = useRef<BoardWebSocket | null>(null);

  useEffect(() => {
    if (!boardId) return;

    wsRef.current = new BoardWebSocket(boardId, (msg) => {
      const type = msg.type as string;
      if (type === "snapshot") {
        onSnapshot(msg.data as string);
      } else if (type === "update") {
        onUpdate(msg.data as string);
      } else if (type === "edit-request" && onEditRequest) {
        onEditRequest(msg.viewerId as string);
      } else if (type === "edit-granted" && onEditGranted) {
        onEditGranted(msg.editToken as string);
      } else if (type === "edit-denied" && onEditDenied) {
        onEditDenied();
      } else if (type === "edit-revoked" && onEditRevoked) {
        onEditRevoked();
      }
    });

    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [boardId, onSnapshot, onUpdate, onEditRequest, onEditGranted, onEditDenied, onEditRevoked]);

  return wsRef;
}
