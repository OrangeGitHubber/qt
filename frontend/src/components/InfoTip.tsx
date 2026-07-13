import { useState } from "react";
import { GLOSSARY } from "../glossary";

export default function InfoTip({ k }: { k: keyof typeof GLOSSARY }) {
  const [open, setOpen] = useState(false);
  const entry = GLOSSARY[k];
  if (!entry) return null;
  return (
    <span className="infotip">
      <button
        type="button"
        className="infotip-btn"
        aria-label={`Explain ${entry.term}`}
        onClick={(e) => {
          e.preventDefault();
          setOpen((o) => !o);
        }}
      >
        ?
      </button>
      {open && (
        <span className="infotip-pop" onClick={() => setOpen(false)}>
          <strong>{entry.term}</strong>
          <br />
          {entry.explain}{" "}
          <a href={entry.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
            Learn more →
          </a>
        </span>
      )}
    </span>
  );
}
