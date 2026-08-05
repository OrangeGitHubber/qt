import { ReactNode, useEffect, useRef, useState } from "react";

/** Turns the dialog into a NAMING prompt: one text field, pre-filled and
 *  selected, whose value is handed to onConfirm. Kept on this component rather
 *  than in a sibling so the focus trap, Escape handling and scrim behaviour have
 *  exactly one implementation — a second copy is the thing that drifts. */
export interface ConfirmPrompt {
  label: string;
  defaultValue: string;
  placeholder?: string;
  maxLength?: number;
}

export interface ConfirmFact {
  label: string;
  value: ReactNode;
  tone?: "up" | "down";
}

/** In-app confirmation for an action worth pausing over.
 *
 *  Replaces window.confirm(), which can't be styled, can't show a table, renders
 *  the site's hostname above your words ("w-qt.oranjehuis.com says"), and puts OK
 *  where the browser feels like putting it — so muscle memory is no defence.
 *
 *  The design principle here: SHOW, don't shout. A wall of red trains you to
 *  click through red walls. The facts are the warning — symbol, size, whose
 *  position it is, what it's currently worth — and exactly one line says what
 *  can't be taken back.
 */
export default function ConfirmDialog({
  open,
  title,
  facts = [],
  warning,
  confirmLabel,
  cancelLabel = "Cancel",
  danger = false,
  busy = false,
  prompt,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  facts?: ConfirmFact[];
  warning?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  danger?: boolean;
  busy?: boolean;
  prompt?: ConfirmPrompt;
  /** Receives the prompt's text when there is one. Callers that don't prompt can
   *  keep taking no arguments — a zero-arg function is assignable here. */
  onConfirm: (value: string) => void;
  onCancel: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [text, setText] = useState("");
  // Whatever you were on before this opened, so focus can go back there rather
  // than to the top of the document.
  const returnTo = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    returnTo.current = document.activeElement;
    if (prompt) {
      // A prompt is a CONSTRUCTIVE action the user opened in order to type, so
      // the field takes focus and its text is selected: Enter accepts the
      // suggestion, typing replaces it. Neither costs a click.
      setText(prompt.defaultValue);
      // …then select on the NEXT frame. setText is async, so at this point the
      // input still holds the previous value and select() would highlight an
      // empty field — which looked right in a screenshot and did nothing when
      // you typed.
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      });
    } else {
      // CANCEL takes focus, not confirm. A destructive button under the default
      // focus ring turns a reflexive Enter into a sold position.
      cancelRef.current?.focus();
    }

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
        return;
      }
      if (e.key !== "Tab") return;
      // Keep Tab inside the dialog: focus that wanders behind the scrim leaves
      // a keyboard user pressing Enter on something they can't see.
      const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      (returnTo.current as HTMLElement | null)?.focus?.();
    };
  }, [open, onCancel, prompt]);

  if (!open) return null;

  return (
    <div
      className="confirm-scrim"
      // Clicking away cancels — the safe outcome. Only on the scrim itself, so a
      // drag that ends outside the panel doesn't count as a dismissal.
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div
        ref={panelRef}
        className={`confirm-panel${danger ? " confirm-danger" : ""}`}
        // alertdialog, not dialog: this interrupts to prevent a mistake, and
        // screen readers should announce it rather than wait to be asked.
        // A prompt asks a question; alertdialog is for interrupting a mistake.
        role={prompt ? "dialog" : "alertdialog"}
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby={warning ? "confirm-warning" : undefined}
      >
        <h3 id="confirm-title" className="confirm-title">
          {title}
        </h3>

        {facts.length > 0 && (
          <dl className="confirm-facts">
            {facts.map((f) => (
              <div key={f.label} className="confirm-fact">
                <dt>{f.label}</dt>
                <dd className={f.tone ?? ""}>{f.value}</dd>
              </div>
            ))}
          </dl>
        )}

        {prompt && (
          <label className="confirm-prompt">
            <span>{prompt.label}</span>
            <input
              ref={inputRef}
              type="text"
              value={text}
              maxLength={prompt.maxLength ?? 80}
              placeholder={prompt.placeholder}
              onChange={(e) => setText(e.target.value)}
              // Enter submits from the field. Without this the only way to
              // accept is to leave the keyboard for the button, which defeats
              // the point of pre-selecting the text.
              onKeyDown={(e) => {
                if (e.key === "Enter" && text.trim() && !busy) {
                  e.preventDefault();
                  onConfirm(text.trim());
                }
              }}
            />
          </label>
        )}

        {warning && (
          <p id="confirm-warning" className="confirm-warning">
            {warning}
          </p>
        )}

        <div className="confirm-actions">
          <button type="button" ref={cancelRef} className="btn-ghost" disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={danger ? "danger-solid" : ""}
            // A blank name is worse than the default one it replaced, so the
            // action is unavailable rather than silently falling back.
            disabled={busy || (!!prompt && !text.trim())}
            onClick={() => onConfirm(text.trim())}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
