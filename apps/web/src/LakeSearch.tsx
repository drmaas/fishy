import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { searchLakes, type LakeSearchHit } from "./lakeApi.ts";

interface LakeSearchProps {
  stateId: string;
  onSelect: (hit: LakeSearchHit) => void;
}

export function LakeSearch({ stateId, onSelect }: LakeSearchProps) {
  const listId = useId();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<LakeSearchHit[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [status, setStatus] = useState<"idle" | "loading" | "empty" | "ready">("idle");
  const requestId = useRef(0);

  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length < 2) {
      setHits([]);
      setStatus("idle");
      setOpen(false);
      return undefined;
    }
    const id = ++requestId.current;
    setStatus("loading");
    const timer = window.setTimeout(() => {
      void searchLakes(stateId, trimmed).then((next) => {
        if (id !== requestId.current) return;
        setHits(next);
        setActive(0);
        setStatus(next.length === 0 ? "empty" : "ready");
        setOpen(true);
      });
    }, 220);
    return () => window.clearTimeout(timer);
  }, [query, stateId]);

  function choose(hit: LakeSearchHit) {
    onSelect(hit);
    setQuery(hit.lake_name);
    setOpen(false);
    setHits([]);
    setStatus("idle");
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      setQuery("");
      setOpen(false);
      setHits([]);
      setStatus("idle");
      return;
    }
    if (!open || hits.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => (index + 1) % hits.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (index - 1 + hits.length) % hits.length);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const hit = hits[active];
      if (hit) choose(hit);
    }
  }

  return (
    <div className="search">
      <label className="search-label" htmlFor="lake-search">
        Find a lake
      </label>
      <input
        id="lake-search"
        type="search"
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={open && hits[active] ? `${listId}-${hits[active].dowlknum}` : undefined}
        placeholder="Name or DOW id"
        value={query}
        autoComplete="off"
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={onKeyDown}
        onFocus={() => {
          if (hits.length > 0 || status === "empty") setOpen(true);
        }}
      />
      {open && (
        <ul id={listId} className="search-results" role="listbox" aria-label="Matching lakes">
          {status === "loading" && <li className="search-status">Searching…</li>}
          {status === "empty" && <li className="search-status">No surveyed lake matches.</li>}
          {hits.map((hit, index) => (
            <li key={hit.dowlknum} role="option" aria-selected={index === active} id={`${listId}-${hit.dowlknum}`}>
              <button
                type="button"
                className={index === active ? "active" : undefined}
                onMouseEnter={() => setActive(index)}
                onClick={() => choose(hit)}
              >
                <span className="search-name">{hit.lake_name}</span>
                <span className="search-meta">
                  {hit.cty_name ? `${hit.cty_name} · ` : ""}
                  {Math.round(hit.acres).toLocaleString()} acres
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
