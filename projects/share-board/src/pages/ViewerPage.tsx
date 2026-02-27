import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { Excalidraw } from "@excalidraw/excalidraw";
import type { ExcalidrawElement } from "@excalidraw/excalidraw/element/types";
import type {
  ExcalidrawImperativeAPI,
  AppState,
  BinaryFiles,
} from "@excalidraw/excalidraw/types";
import { useLiveSync } from "../hooks/useLiveSync";
import { getBoard, updateBoard } from "../lib/api";

type EditRequestStatus = "idle" | "pending" | "granted" | "denied";

interface LaserPoint {
  x: number;
  y: number;
  timestamp: number;
}

export function ViewerPage() {
  const { id } = useParams<{ id: string }>();
  const apiRef = useRef<ExcalidrawImperativeAPI | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [initialData, setInitialData] = useState<{
    elements: ExcalidrawElement[];
    files: BinaryFiles;
  } | null>(null);
  const [editStatus, setEditStatus] = useState<EditRequestStatus>("idle");
  const [editToken, setEditToken] = useState<string | null>(null);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [laserPoints, setLaserPoints] = useState<LaserPoint[]>([]);
  const appStateRef = useRef<AppState | null>(null);
  const laserCleanupRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Cleanup old laser points every 50ms
  useEffect(() => {
    laserCleanupRef.current = setInterval(() => {
      const now = Date.now();
      setLaserPoints((prev) => {
        const filtered = prev.filter((p) => now - p.timestamp < 1500);
        if (filtered.length === prev.length) return prev;
        return filtered;
      });
    }, 50);

    return () => {
      if (laserCleanupRef.current) clearInterval(laserCleanupRef.current);
    };
  }, []);

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

  const handleLaser = useCallback((data: string) => {
    try {
      const point = JSON.parse(data) as { x: number; y: number };
      setLaserPoints((prev) => [
        ...prev,
        { x: point.x, y: point.y, timestamp: Date.now() },
      ]);
    } catch {
      // Ignore malformed laser data
    }
  }, []);

  const handleEditGranted = useCallback((token: string) => {
    setEditStatus("granted");
    setEditToken(token);
  }, []);

  const handleEditDenied = useCallback(() => {
    setEditStatus("denied");
    setTimeout(() => setEditStatus("idle"), 3000);
  }, []);

  const handleEditRevoked = useCallback(() => {
    setEditStatus("idle");
    setEditToken(null);
  }, []);

  // Live sync via WebSocket
  const wsRef = useLiveSync(
    loaded ? id ?? null : null,
    handleSnapshot,
    handleUpdate,
    handleLaser,
    undefined, // onEditRequest not needed for viewer
    handleEditGranted,
    handleEditDenied,
    handleEditRevoked,
  );

  const handleRequestEdit = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.sendEditRequest();
      setEditStatus("pending");
    }
  }, [wsRef]);

  const getSnapshot = useCallback(() => {
    if (!apiRef.current) return null;
    return {
      elements: apiRef.current.getSceneElements(),
      files: apiRef.current.getFiles(),
    };
  }, []);

  const handleChange = useCallback(
    (
      _elements: readonly ExcalidrawElement[],
      appState: AppState,
      _files: BinaryFiles,
    ) => {
      // Track appState for coordinate conversion
      appStateRef.current = appState;

      if (!editToken || !id || !wsRef.current) return;

      const snapshot = getSnapshot();
      if (snapshot) {
        wsRef.current.sendUpdate(editToken, JSON.stringify(snapshot));
      }

      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      saveTimerRef.current = setTimeout(async () => {
        const snap = getSnapshot();
        if (snap) {
          try {
            await updateBoard(id, editToken, snap);
          } catch {
            // Ignore save errors in viewer edit mode
          }
        }
      }, 2000);
    },
    [editToken, id, getSnapshot, wsRef],
  );

  // Convert scene coordinates to screen coordinates for rendering
  const getScreenCoords = useCallback(
    (sceneX: number, sceneY: number) => {
      const state = appStateRef.current;
      if (!state) return { x: sceneX, y: sceneY };
      const { scrollX, scrollY, zoom } = state;
      return {
        x: (sceneX + scrollX) * zoom.value,
        y: (sceneY + scrollY) * zoom.value,
      };
    },
    [],
  );

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

  const isEditing = editStatus === "granted" && editToken;

  return (
    <div className="viewer-page">
      <div className="viewer-topbar">
        <span className={`viewer-badge ${isEditing ? "viewer-badge-edit" : ""}`}>
          {isEditing ? "Editing" : "View Only"}
        </span>
        <div className="viewer-topbar-right">
          {editStatus === "idle" && (
            <button className="request-edit-btn" onClick={handleRequestEdit}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path
                  d="M11.5 1.5l3 3-9 9H2.5v-3l9-9z"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Request to Edit
            </button>
          )}
          {editStatus === "pending" && (
            <span className="edit-status-pending">
              <span className="spinner spinner-sm" />
              Edit request sent, waiting for approval...
            </span>
          )}
          {editStatus === "denied" && (
            <span className="edit-status-denied">
              Edit request denied
            </span>
          )}
          {isEditing && (
            <span className="edit-status-granted">
              Edit access granted
            </span>
          )}
          <Link to="/" className="viewer-new-btn">
            + New Board
          </Link>
        </div>
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
            viewModeEnabled={!isEditing}
            onChange={handleChange}
          />
        )}
        {laserPoints.length > 0 && (
          <svg className="laser-overlay">
            {laserPoints.length >= 2 &&
              laserPoints.slice(-20).map((point, i, arr) => {
                if (i === 0) return null;
                const prev = arr[i - 1];
                const fromCoords = getScreenCoords(prev.x, prev.y);
                const toCoords = getScreenCoords(point.x, point.y);
                const age = Date.now() - point.timestamp;
                const opacity = Math.max(0, 1 - age / 1500);
                return (
                  <line
                    key={`${point.timestamp}-${i}`}
                    x1={fromCoords.x}
                    y1={fromCoords.y}
                    x2={toCoords.x}
                    y2={toCoords.y}
                    stroke="rgba(232, 55, 55, 0.8)"
                    strokeWidth="3"
                    strokeLinecap="round"
                    opacity={opacity}
                  />
                );
              })}
            {(() => {
              const lastPoint = laserPoints[laserPoints.length - 1];
              const age = Date.now() - lastPoint.timestamp;
              if (age > 1500) return null;
              const coords = getScreenCoords(lastPoint.x, lastPoint.y);
              const opacity = Math.max(0, 1 - age / 1500);
              return (
                <>
                  <circle
                    cx={coords.x}
                    cy={coords.y}
                    r="8"
                    fill="rgba(232, 55, 55, 0.3)"
                    opacity={opacity}
                  />
                  <circle
                    cx={coords.x}
                    cy={coords.y}
                    r="4"
                    fill="rgba(232, 55, 55, 0.9)"
                    opacity={opacity}
                  />
                </>
              );
            })()}
          </svg>
        )}
      </div>
    </div>
  );
}
