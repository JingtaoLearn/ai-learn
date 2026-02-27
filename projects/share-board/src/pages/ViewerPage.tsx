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
import { getBoard } from "../lib/api";

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

  const handleExcalidrawChange = useCallback(
    (_elements: readonly ExcalidrawElement[], appState: AppState, _files: BinaryFiles) => {
      appStateRef.current = appState;
    },
    [],
  );

  // Live sync via WebSocket
  useLiveSync(loaded ? id ?? null : null, handleSnapshot, handleUpdate, handleLaser);

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
            onChange={handleExcalidrawChange}
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
