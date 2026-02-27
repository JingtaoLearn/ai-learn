interface ToolbarProps {
  onShare: () => void;
  saving: boolean;
  laserActive?: boolean;
  onLaserToggle?: () => void;
}

export function Toolbar({ onShare, saving, laserActive, onLaserToggle }: ToolbarProps) {
  return (
    <div className="toolbar">
      <div className="toolbar-left">
        <span className="toolbar-logo">Share Board</span>
      </div>
      <div className="toolbar-right">
        {onLaserToggle && (
          <button
            className={`laser-btn${laserActive ? " laser-btn-active" : ""}`}
            onClick={onLaserToggle}
            title="Laser Pointer"
          >
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <circle cx="9" cy="4" r="2" stroke="currentColor" strokeWidth="1.5" />
              <line x1="9" y1="6" x2="9" y2="16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              <line x1="6" y1="14" x2="9" y2="16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              <line x1="12" y1="14" x2="9" y2="16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            Laser
          </button>
        )}
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
