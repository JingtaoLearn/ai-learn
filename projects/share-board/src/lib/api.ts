import type {
  Board,
  CreateBoardResponse,
  UpdateBoardResponse,
  AssetUploadResponse,
} from "../types/board";

const API_BASE = "/api";

export async function createBoard(
  snapshot: object,
): Promise<CreateBoardResponse> {
  const res = await fetch(`${API_BASE}/boards`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ snapshot }),
  });
  if (!res.ok) throw new Error("Failed to create board");
  return res.json();
}

export async function getBoard(id: string): Promise<Board> {
  const res = await fetch(`${API_BASE}/boards/${id}`);
  if (!res.ok) throw new Error("Board not found");
  return res.json();
}

export async function updateBoard(
  id: string,
  editToken: string,
  snapshot: object,
): Promise<UpdateBoardResponse> {
  const res = await fetch(`${API_BASE}/boards/${id}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      "X-Edit-Token": editToken,
    },
    body: JSON.stringify({ snapshot }),
  });
  if (!res.ok) throw new Error("Failed to update board");
  return res.json();
}

export async function uploadAsset(
  boardId: string,
  editToken: string,
  file: File,
): Promise<AssetUploadResponse> {
  const formData = new FormData();
  formData.append("file", file);

  const res = await fetch(`${API_BASE}/boards/${boardId}/assets`, {
    method: "POST",
    headers: { "X-Edit-Token": editToken },
    body: formData,
  });
  if (!res.ok) throw new Error("Failed to upload asset");
  return res.json();
}
