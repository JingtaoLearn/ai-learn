import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Excalidraw } from "@excalidraw/excalidraw";
import type { ExcalidrawElement } from "@excalidraw/excalidraw/element/types";
import type {
  ExcalidrawImperativeAPI,
  AppState,
  BinaryFiles,
} from "@excalidraw/excalidraw/types";
import { Toolbar } from "../components/Toolbar";
import { ShareDialog } from "../components/ShareDialog";
import { EditRequestNotification } from "../components/EditRequestNotification";
import { useBoard } from "../hooks/useBoard";
import { BoardWebSocket } from "../lib/ws";

interface PendingEditRequest {
  viewerId: string;
  timestamp: number;
}

export function EditorPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { boardId, setBoardId, loading, saveBoard, loadBoard, getEditToken } =
    useBoard();
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const [showShare, setShowShare] = useState(false);
  const [laserActive, setLaserActive] = useState(false);
  const apiRef = useRef<ExcalidrawImperativeAPI | null>(null);
  const wsRef = useRef<BoardWebSocket | null>(null);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const initialDataRef = useRef<{
    elements: readonly ExcalidrawElement[];
    files: BinaryFiles;
  } | null>(null);
  const [initialDataReady, setInitialDataReady] = useState(!id);
  const [editRequests, setEditRequests] = useState<PendingEditRequest[]>([]);
  const laserActiveRef = useRef(false);
  const appStateRef = useRef<AppState | null>(null);

  const editToken = boardId ? getEditToken(boardId) : null;

  // Keep ref in sync with state
  useEffect(() => {
    laserActiveRef.current = laserActive;
  }, [laserActive]);

  // Load existing board if editing
  useEffect(() => {
    if (!id) return;

    const token = localStorage.getItem(`board:${id}:editToken`);
    if (!token) {
      navigate(`/view/${id}`, { replace: true });
      return;
    }

    setBoardId(id);
    loadBoard(id).then((board) => {
      if (board) {
        const snapshot =
          typeof board.snapshot === "string"
            ? JSON.parse(board.snapshot)
            : board.snapshot;
        initialDataRef.current = {
          elements: snapshot.elements || [],
          files: snapshot.files || {},
        };
        if (apiRef.current) {
          apiRef.current.updateScene({
            elements: snapshot.elements || [],
          });
          if (snapshot.files) {
            apiRef.current.addFiles(
              Object.values(snapshot.files) as any[],
            );
          }
        }
      }
      setInitialDataReady(true);
    });
  }, [id, loadBoard, navigate, setBoardId]);

  // Setup WebSocket for live sync when editing a saved board
  useEffect(() => {
    if (!boardId || !editToken) return;

    wsRef.current = new BoardWebSocket(boardId, (msg) => {
      const type = msg.type as string;
      if (type === "edit-request") {
        const viewerId = msg.viewerId as string;
        setEditRequests((prev) => {
          if (prev.some((r) => r.viewerId === viewerId)) return prev;
          return [...prev, { viewerId, timestamp: Date.now() }];
        });
      }
    });

    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [boardId, editToken]);

  const handleApproveEdit = useCallback(
    (viewerId: string) => {
      if (wsRef.current && editToken) {
        wsRef.current.sendEditResponse(editToken, viewerId, true);
        setEditRequests((prev) => prev.filter((r) => r.viewerId !== viewerId));
      }
    },
    [editToken],
  );

  const handleDenyEdit = useCallback(
    (viewerId: string) => {
      if (wsRef.current && editToken) {
        wsRef.current.sendEditResponse(editToken, viewerId, false);
        setEditRequests((prev) => prev.filter((r) => r.viewerId !== viewerId));
      }
    },
    [editToken],
  );

  // Laser pointer mouse tracking
  useEffect(() => {
    if (!laserActive) return;

    const canvasEl = document.querySelector(".editor-canvas");
    if (!canvasEl) return;

    const handleMouseMove = (e: Event) => {
      const mouseEvent = e as MouseEvent;
      if (!wsRef.current || !appStateRef.current) return;

      const currentBoardId = localStorage.getItem("share-board:currentId");
      const currentToken = currentBoardId
        ? localStorage.getItem(`board:${currentBoardId}:editToken`)
        : null;
      if (!currentToken) return;

      const rect = (canvasEl as HTMLElement).getBoundingClientRect();
      const screenX = mouseEvent.clientX - rect.left;
      const screenY = mouseEvent.clientY - rect.top;

      // Convert screen coordinates to scene coordinates using Excalidraw's appState
      const { scrollX, scrollY, zoom } = appStateRef.current;
      const sceneX = (screenX / zoom.value) - scrollX;
      const sceneY = (screenY / zoom.value) - scrollY;

      wsRef.current.sendLaser(
        currentToken,
        JSON.stringify({ x: sceneX, y: sceneY }),
      );
    };

    canvasEl.addEventListener("mousemove", handleMouseMove);
    return () => {
      canvasEl.removeEventListener("mousemove", handleMouseMove);
    };
  }, [laserActive]);

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

      const currentBoardId = localStorage.getItem("share-board:currentId");
      const currentToken = currentBoardId
        ? localStorage.getItem(`board:${currentBoardId}:editToken`)
        : null;

      if (currentBoardId && currentToken && wsRef.current) {
        const snapshot = getSnapshot();
        if (snapshot) {
          wsRef.current.sendUpdate(
            currentToken,
            JSON.stringify(snapshot),
          );
        }
      }

      // Debounced auto-save
      if (currentBoardId && currentToken) {
        if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
        saveTimerRef.current = setTimeout(() => {
          const snapshot = getSnapshot();
          if (snapshot) {
            saveBoard(snapshot);
          }
        }, 2000);
      }
    },
    [getSnapshot, saveBoard],
  );

  const handleShare = useCallback(async () => {
    const snapshot = getSnapshot();
    if (!snapshot) return;

    const result = await saveBoard(snapshot);

    if (result) {
      setShareUrl(result.shareUrl);
      setShowShare(true);

      localStorage.setItem("share-board:currentId", result.id);

      if (!id) {
        navigate(`/edit/${result.id}`, { replace: true });
      }
    }
  }, [getSnapshot, saveBoard, id, navigate]);

  const handleLaserToggle = useCallback(() => {
    setLaserActive((prev) => !prev);
  }, []);

  return (
    <div className="editor-page">
      <Toolbar
        onShare={handleShare}
        saving={loading}
        laserActive={laserActive}
        onLaserToggle={handleLaserToggle}
      />
      <div className={`editor-canvas${laserActive ? " laser-cursor" : ""}`}>
        {initialDataReady && (
          <Excalidraw
            excalidrawAPI={(api) => {
              apiRef.current = api;
            }}
            initialData={
              initialDataRef.current
                ? {
                    elements:
                      initialDataRef.current.elements as ExcalidrawElement[],
                    files: initialDataRef.current.files,
                  }
                : undefined
            }
            onChange={handleChange}
          />
        )}
      </div>
      {showShare && shareUrl && (
        <ShareDialog
          shareUrl={shareUrl}
          onClose={() => setShowShare(false)}
        />
      )}
      {editRequests.map((request) => (
        <EditRequestNotification
          key={request.viewerId}
          viewerId={request.viewerId}
          onApprove={handleApproveEdit}
          onDeny={handleDenyEdit}
        />
      ))}
    </div>
  );
}
