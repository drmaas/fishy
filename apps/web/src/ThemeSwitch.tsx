import type { ThemePreference } from "./theme.ts";

const OPTIONS: ThemePreference[] = ["light", "dark", "auto"];

const LABELS: Record<ThemePreference, string> = {
  light: "Light",
  dark: "Dark",
  auto: "Auto",
};

interface ThemeSwitchProps {
  value: ThemePreference;
  onChange: (value: ThemePreference) => void;
}

export function ThemeSwitch({ value, onChange }: ThemeSwitchProps) {
  return (
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
            {LABELS[option]}
          </button>
        );
      })}
    </div>
  );
}
