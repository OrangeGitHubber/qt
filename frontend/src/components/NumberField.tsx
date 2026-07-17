import { useEffect, useState } from "react";

/** A number input you can actually clear and retype.
 *
 *  A naive `value={n} onChange={e => set(Number(e.target.value))}` turns an
 *  empty box into 0 instantly (Number("") === 0), so the 0 re-appears before
 *  you can type over it. This keeps your literal keystrokes while focused,
 *  commits upward only when they parse as a number, and restores the last
 *  good value if you leave the field empty.
 */
export default function NumberField({
  value,
  onChange,
  min,
  max,
  step,
  required,
}: {
  value: number;
  onChange: (n: number) => void;
  min?: number | string;
  max?: number | string;
  step?: number | string;
  required?: boolean;
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
