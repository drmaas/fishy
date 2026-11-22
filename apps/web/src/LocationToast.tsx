import {
  locationBannerShowDismiss,
  locationBannerShowRetry,
  type LocationBannerModel,
} from "./locationBanner.ts";
import { locationHint } from "./locationHint.ts";

interface LocationToastProps {
  model: LocationBannerModel;
  onDismiss: () => void;
  onRetry: () => void;
}

export function LocationToast({ model, onDismiss, onRetry }: LocationToastProps) {
  const text = locationHint(model.location);
  if (!text) return null;

  const showRetry = locationBannerShowRetry(model);
  const showDismiss = locationBannerShowDismiss(model);
  const isError = model.location.status === "error";

  return (
    <div
      className="location-toast hint"
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
    >
      <p className="location-toast-text">{text}</p>
      {(showRetry || showDismiss) && (
        <div className="location-toast-actions">
          {showRetry && (
            <button type="button" className="tap-target" onClick={onRetry}>
              Try again
            </button>
          )}
          {showDismiss && (
            <button
              type="button"
              className="tap-target location-toast-close"
              onClick={onDismiss}
              aria-label="Dismiss location message"
            >
              Close
            </button>
          )}
        </div>
      )}
    </div>
  );
}