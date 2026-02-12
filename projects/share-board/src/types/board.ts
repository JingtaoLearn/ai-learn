export interface Board {
  id: string;
  snapshot: string;
  updatedAt: string;
}

export interface CreateBoardResponse {
  id: string;
  editToken: string;
  shareUrl: string;
}

export interface UpdateBoardResponse {
  ok: true;
}

export interface AssetUploadResponse {
  url: string;
}

export interface WsMessage {
  type: "snapshot" | "update";
  data: string;
}

export interface WsUpdateMessage {
  type: "update";
  editToken: string;
  data: string;
}
