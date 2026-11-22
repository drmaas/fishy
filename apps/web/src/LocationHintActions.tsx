interface LocationHintActionsProps {
  onRetry: () => void;
  onDismiss: () => void;
  className?: string;
}

export function LocationHintActions({ onRetry, onDismiss, className }: LocationHintActionsProps) {
  return (
    <div className={className ?? "location-hint-actions"}>
      <button type="button" className="tap-target" onClick={onRetry}>
        Try again
      </button>
      <button
        type="button"
        className="tap-target location-hint-close"
        onClick={onDismiss}
        aria-label="Dismiss location message"
      >
        Close
      </button>
    </div>
  );
}