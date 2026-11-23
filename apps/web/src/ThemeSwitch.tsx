import {
  cycleThemePreference,
  themePreferenceLabel,
  type ThemePreference,
} from "./theme.ts";

const OPTIONS: ThemePreference[] = ["light", "dark", "auto"];

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" width={20} height={20} aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="4.25" fill="none" stroke="currentColor" strokeWidth="1.75" />
      <path
        d="M12 3.25v2.5M12 18.25v2.5M4.75 12h2.5M16.75 12h2.5M6.5 6.5l1.75 1.75M15.75 15.75l1.75 1.75M17.5 6.5l-1.75 1.75M8.25 15.75l-1.75 1.75"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" width={20} height={20} aria-hidden="true" focusable="false">
      <path
        d="M18.5 14.2a7.25 7.25 0 0 1-9.75-9.75 6.6 6.6 0 1 0 9.75 9.75z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function AutoIcon() {
  return (
    <svg viewBox="0 0 24 24" width={20} height={20} aria-hidden="true" focusable="false">
      <path
        d="M12 4.5a7.5 7.5 0 1 0 0 15"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
      <path d="M12 4.5v5.25h5.25" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}

function ThemeIcon({ preference }: { preference: ThemePreference }) {
  if (preference === "light") return <SunIcon />;
  if (preference === "dark") return <MoonIcon />;
  return <AutoIcon />;
}

export interface ThemeIconButtonProps {
  value: ThemePreference;
  onChange: (value: ThemePreference) => void;
  className?: string;
}

export function ThemeIconButton({ value, onChange, className }: ThemeIconButtonProps) {
  const label = themePreferenceLabel(value);
  const classes = ["theme-switch-icon", "tap-target", className].filter(Boolean).join(" ");

  return (
    <button
      type="button"
      className={classes}
      onClick={() => onChange(cycleThemePreference(value))}
      aria-label={`Color scheme: ${label}. Activate to switch.`}
      title={`${label} mode — tap to switch`}
    >
      <ThemeIcon preference={value} />
    </button>
  );
}

interface ThemeSwitchProps {
  value: ThemePreference;
  onChange: (value: ThemePreference) => void;
}

export function ThemeSwitch({ value, onChange }: ThemeSwitchProps) {
  return (
    <div className="theme-controls">
      <div className="theme-switch" role="radiogroup" aria-label="Color scheme">
        {OPTIONS.map((option) => {
          const selected = value === option;
          return (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={selected}
              className={selected ? "theme-switch-option is-selected" : "theme-switch-option"}
              onClick={() => onChange(option)}
            >
              {themePreferenceLabel(option)}
            </button>
          );
        })}
      </div>
      <ThemeIconButton value={value} onChange={onChange} />
    </div>
  );
}