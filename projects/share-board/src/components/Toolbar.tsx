interface ToolbarProps {
  onShare: () => void;
  saving: boolean;
}

export function Toolbar({ onShare, saving }: ToolbarProps) {
  return (
    <div className="toolbar">
      <div className="toolbar-left">
        <span className="toolbar-logo">Share Board</span>
      </div>
      <div className="toolbar-right">
        <button
          className="share-btn"
          onClick={onShare}
          disabled={saving}
        >
          {saving ? (
            <>
              <span className="spinner" />
              Saving...
            </>
          ) : (
            <>
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                <path
                  d="M13.5 6.75a2.25 2.25 0 100-4.5 2.25 2.25 0 000 4.5zM4.5 11.25a2.25 2.25 0 100-4.5 2.25 2.25 0 000 4.5zM13.5 15.75a2.25 2.25 0 100-4.5 2.25 2.25 0 000 4.5zM6.44 10.24l5.13 2.77M11.56 5.74l-5.12 2.77"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Share
            </>
          )}
        </button>
      </div>
    </div>
  );
}
