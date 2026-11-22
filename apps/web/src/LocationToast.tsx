import { LocationHintActions } from "./LocationHintActions.tsx";
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
      {showRetry && showDismiss && (
        <LocationHintActions onRetry={onRetry} onDismiss={onDismiss} className="location-toast-actions" />
      )}
    </div>
  );
}