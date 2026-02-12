import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { Excalidraw } from "@excalidraw/excalidraw";
import type { ExcalidrawElement } from "@excalidraw/excalidraw/element/types";
import type {
  ExcalidrawImperativeAPI,
  BinaryFiles,
} from "@excalidraw/excalidraw/types";
import { useLiveSync } from "../hooks/useLiveSync";
import { getBoard } from "../lib/api";

export function ViewerPage() {
  const { id } = useParams<{ id: string }>();
  const apiRef = useRef<ExcalidrawImperativeAPI | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [initialData, setInitialData] = useState<{
    elements: ExcalidrawElement[];
    files: BinaryFiles;
  } | null>(null);

  // Load initial board data
  useEffect(() => {
    if (!id) return;

    getBoard(id)
      .then((board) => {
        const snapshot =
          typeof board.snapshot === "string"
            ? JSON.parse(board.snapshot)
            : board.snapshot;
        setInitialData({
          elements: snapshot.elements || [],
          files: snapshot.files || {},
        });
        setLoaded(true);
      })
      .catch(() => {
        setError("Board not found");
      });
  }, [id]);

  const handleSnapshot = useCallback((data: string) => {
    if (!apiRef.current) return;
    try {
      const snapshot = JSON.parse(data);
      apiRef.current.updateScene({
        elements: snapshot.elements || [],
      });
      if (snapshot.files) {
        apiRef.current.addFiles(Object.values(snapshot.files) as any[]);
      }
    } catch {
      // Ignore malformed snapshots
    }
  }, []);

  const handleUpdate = useCallback((data: string) => {
    if (!apiRef.current) return;
    try {
      const snapshot = JSON.parse(data);
      apiRef.current.updateScene({
        elements: snapshot.elements || [],
      });
      if (snapshot.files) {
        apiRef.current.addFiles(Object.values(snapshot.files) as any[]);
      }
    } catch {
      // Ignore malformed updates
    }
  }, []);

  // Live sync via WebSocket
  useLiveSync(loaded ? id ?? null : null, handleSnapshot, handleUpdate);

  if (error) {
    return (
      <div className="viewer-error">
        <div className="viewer-error-card">
          <h2>Board Not Found</h2>
          <p>This board may have been deleted or the link is incorrect.</p>
          <Link to="/" className="viewer-error-link">
            Create a New Board
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="viewer-page">
      <div className="viewer-topbar">
        <span className="viewer-badge">View Only</span>
        <Link to="/" className="viewer-new-btn">
          + New Board
        </Link>
      </div>
      <div className="viewer-canvas">
        {initialData && (
          <Excalidraw
            excalidrawAPI={(api) => {
              apiRef.current = api;
            }}
            initialData={{
              elements: initialData.elements,
              files: initialData.files,
            }}
            viewModeEnabled={true}
          />
        )}
      </div>
    </div>
  );
}
