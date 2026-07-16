import { useEffect, useRef, useState } from "react";
import { GLOSSARY } from "../glossary";

export default function InfoTip({ k }: { k: keyof typeof GLOSSARY }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const entry = GLOSSARY[k];

  useEffect(() => {
    if (!open) return;
    // Any click outside dismisses it — no hunting for the same "?" again.
    // Clicks inside are handled by the popup's own onClick (the link opts out).
    const onPointerDown = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("blur", () => setOpen(false), { once: true });
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  if (!entry) return null;
  return (
    <span className="infotip" ref={ref}>
      <button
        type="button"
        className="infotip-btn"
        aria-label={`Explain ${entry.term}`}
        aria-expanded={open}
        onClick={(e) => {
          e.preventDefault();
          setOpen((o) => !o);
        }}
      >
        ?
      </button>
      {open && (
        <span className="infotip-pop" role="tooltip" onClick={() => setOpen(false)}>
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
