import { useEffect, useRef, useState } from "react";
import { GLOSSARY } from "../glossary";

const POP_WIDTH = 280; // keep in sync with .infotip-pop width

export default function InfoTip({ k }: { k: keyof typeof GLOSSARY }) {
  const [open, setOpen] = useState(false);
  // Viewport coordinates for the FIXED-position popover. Fixed (not absolute)
  // so it escapes overflow containers — inside a scrolling table an absolute
  // popover gets clipped and grows ugly scrollbars around the table instead.
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  const entry = GLOSSARY[k];

  function toggle() {
    if (!open) {
      const r = ref.current?.getBoundingClientRect();
      if (r) {
        // Clamp horizontally so a "?" near the right edge doesn't overflow.
        const left = Math.min(Math.max(8, r.left), window.innerWidth - POP_WIDTH - 8);
        setPos({ top: r.bottom + 6, left });
      }
    }
    setOpen((o) => !o);
  }

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
    // A fixed popover doesn't follow its anchor — close on any scroll (capture
    // catches inner scroll containers like the tables) instead of drifting.
    const onScroll = () => setOpen(false);
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("blur", () => setOpen(false), { once: true });
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("scroll", onScroll, { capture: true });
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
          toggle();
        }}
      >
        ?
      </button>
      {open && pos && (
        <span
          className="infotip-pop"
          role="tooltip"
          style={{ top: pos.top, left: pos.left }}
          onClick={() => setOpen(false)}
        >
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
