import { useMemo } from "react";
import { uploadAsset } from "../lib/api";

export interface AssetUploader {
  upload: (file: File) => Promise<{ url: string }>;
}

export function useAssetUpload(
  boardId: string | null,
  editToken: string | null,
): AssetUploader {
  return useMemo<AssetUploader>(
    () => ({
      async upload(file: File) {
        if (!boardId || !editToken) {
          throw new Error("Board must be saved before uploading assets");
        }
        return uploadAsset(boardId, editToken, file);
      },
    }),
    [boardId, editToken],
  );
}
