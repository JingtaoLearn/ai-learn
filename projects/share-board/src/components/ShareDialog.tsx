import { useState } from "react";

interface ShareDialogProps {
  shareUrl: string;
  onClose: () => void;
}

export function ShareDialog({ shareUrl, onClose }: ShareDialogProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback: select the input text
      const input = document.querySelector<HTMLInputElement>(".share-url-input");
      input?.select();
    }
  };

  return (
    <div className="share-overlay" onClick={onClose}>
      <div className="share-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="share-dialog-header">
          <h2>Share Your Board</h2>
          <button className="share-close-btn" onClick={onClose}>
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
              <path
                d="M15 5L5 15M5 5l10 10"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>

        <p className="share-description">
          Anyone with this link can view your board
        </p>

        <div className="share-url-row">
          <input
            className="share-url-input"
            readOnly
            value={shareUrl}
            onFocus={(e) => e.target.select()}
          />
          <button className="share-copy-btn" onClick={handleCopy}>
            {copied ? (
              <>
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <path
                    d="M3 8.5l3 3 7-7"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
                Copied!
              </>
            ) : (
              <>
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <rect
                    x="5"
                    y="5"
                    width="9"
                    height="9"
                    rx="1.5"
                    stroke="currentColor"
                    strokeWidth="1.5"
                  />
                  <path
                    d="M11 5V3.5A1.5 1.5 0 009.5 2h-6A1.5 1.5 0 002 3.5v6A1.5 1.5 0 003.5 11H5"
                    stroke="currentColor"
                    strokeWidth="1.5"
                  />
                </svg>
                Copy Link
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
