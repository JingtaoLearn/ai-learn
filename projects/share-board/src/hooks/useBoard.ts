import { useState, useCallback } from "react";
import { createBoard, getBoard, updateBoard } from "../lib/api";

function getEditToken(boardId: string): string | null {
  return localStorage.getItem(`board:${boardId}:editToken`);
}

function setEditToken(boardId: string, token: string) {
  localStorage.setItem(`board:${boardId}:editToken`, token);
}

export function useBoard() {
  const [boardId, setBoardId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const saveBoard = useCallback(
    async (snapshot: object): Promise<{ id: string; shareUrl: string } | null> => {
      setLoading(true);
      setError(null);
      try {
        // If we have a boardId and editToken, update existing board
        if (boardId) {
          const editToken = getEditToken(boardId);
          if (editToken) {
            await updateBoard(boardId, editToken, snapshot);
            const baseUrl = window.location.origin;
            return { id: boardId, shareUrl: `${baseUrl}/view/${boardId}` };
          }
        }

        // Otherwise create a new board
        const result = await createBoard(snapshot);
        setBoardId(result.id);
        setEditToken(result.id, result.editToken);
        return { id: result.id, shareUrl: result.shareUrl };
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to save");
        return null;
      } finally {
        setLoading(false);
      }
    },
    [boardId],
  );

  const loadBoard = useCallback(async (id: string) => {
    setLoading(true);
    setError(null);
    try {
      const board = await getBoard(id);
      setBoardId(id);
      return board;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  return {
    boardId,
    setBoardId,
    loading,
    error,
    saveBoard,
    loadBoard,
    getEditToken,
  };
}
