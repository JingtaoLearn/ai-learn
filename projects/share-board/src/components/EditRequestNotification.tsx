interface EditRequestNotificationProps {
  viewerId: string;
  onApprove: (viewerId: string) => void;
  onDeny: (viewerId: string) => void;
}

export function EditRequestNotification({
  viewerId,
  onApprove,
  onDeny,
}: EditRequestNotificationProps) {
  return (
    <div className="edit-request-notification">
      <div className="edit-request-icon">
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
          <path
            d="M14.5 2.5l3 3-9 9H5.5v-3l9-9z"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      <div className="edit-request-content">
        <p className="edit-request-text">
          A viewer is requesting edit access
        </p>
        <p className="edit-request-id">
          Viewer {viewerId.slice(0, 6)}
        </p>
      </div>
      <div className="edit-request-actions">
        <button
          className="edit-request-approve"
          onClick={() => onApprove(viewerId)}
        >
          Approve
        </button>
        <button
          className="edit-request-deny"
          onClick={() => onDeny(viewerId)}
        >
          Deny
        </button>
      </div>
    </div>
  );
}
