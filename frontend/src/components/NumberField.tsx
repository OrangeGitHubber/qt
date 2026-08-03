import { useEffect, useState } from "react";

/** A number input you can actually clear and retype.
 *
 *  A naive `value={n} onChange={e => set(Number(e.target.value))}` turns an
 *  empty box into 0 instantly (Number("") === 0), so the 0 re-appears before
 *  you can type over it. This keeps your literal keystrokes while focused,
 *  commits upward only when they parse as a number, and restores the last
 *  good value if you leave the field empty.
 *
 *  `step` IS A VALIDATION RULE, NOT A SPINNER SETTING. It looks like it only
 *  sizes the up/down arrows, but the browser also refuses to submit the
 *  surrounding form when the value isn't on the step grid (counting from `min`,
 *  not from zero) — with a native message this app cannot see or reword. So
 *  step="0.1" on a percentage silently means "one decimal place only", and any
 *  value with more precision makes the whole form unsavable.
 *
 *  Rule for this codebase: `step` must mirror the BACKEND's type for the field.
 *  Fields the pydantic model declares `int` use step="1"; fields it declares
 *  `float` use step="any", because the model, the backtester and the live engine
 *  all treat them as continuous. Inventing a precision limit here that the server
 *  does not have produces values one part of the app writes and another refuses —
 *  which is exactly what the optimizer's tuned percentages ran into.
 */
export default function NumberField({
  value,
  onChange,
  min,
  max,
  step,
  required,
  disabled,
}: {
  value: number;
  onChange: (n: number) => void;
  min?: number | string;
  max?: number | string;
  step?: number | string;
  required?: boolean;
  disabled?: boolean;
}) {
  const [text, setText] = useState(String(value));
  const [focused, setFocused] = useState(false);

  // Track external changes (e.g. applying a preset) while not being edited.
  useEffect(() => {
    if (!focused) setText(String(value));
  }, [value, focused]);

  return (
    <input
      type="number"
      min={min}
      max={max}
      step={step}
      required={required}
      disabled={disabled}
      value={focused ? text : String(value)}
      onFocus={() => {
        setText(String(value));
        setFocused(true);
      }}
      onChange={(e) => {
        const next = e.target.value;
        setText(next);
        // "" / "-" / "." are valid things to be typing, just not to commit
        if (next !== "" && Number.isFinite(Number(next))) onChange(Number(next));
      }}
      onBlur={() => {
        setFocused(false);
        if (text === "" || !Number.isFinite(Number(text))) setText(String(value));
      }}
    />
  );
}
