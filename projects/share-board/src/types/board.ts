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
  type: "snapshot" | "update" | "edit-request" | "edit-response" | "edit-granted" | "edit-denied" | "edit-revoked";
  data: string;
}

export interface WsUpdateMessage {
  type: "update";
  editToken: string;
  data: string;
}

export interface WsEditRequest {
  type: "edit-request";
  viewerId: string;
}

export interface WsEditResponse {
  type: "edit-response";
  viewerId: string;
  approved: boolean;
  editToken?: string;
}

export interface WsEditGranted {
  type: "edit-granted";
  editToken: string;
}

export interface WsEditDenied {
  type: "edit-denied";
}
