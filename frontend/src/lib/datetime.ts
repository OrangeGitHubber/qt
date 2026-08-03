// The one place that turns a stored instant into a wall clock.
//
// Everything QT stores is UTC and stays UTC — this module never touches what is
// written, only what is read. The zone it renders in is a user setting (default
// America/New_York, because that is the market's own clock and the times worth
// comparing are market times), so the whole UI must go through here rather than
// through `toLocaleString()`: the browser's own zone is the one answer that is
// wrong for everybody except a New Yorker looking at a New York market.
//
// DST is not handled here on purpose. Passing an IANA zone NAME to Intl gets it
// right for free — an offset would silently rot twice a year, which is exactly
// the class of bug that is invisible until a trade looks an hour off.
//
// No dependency: Intl.DateTimeFormat with a `timeZone` is the platform answer.

import { useSyncExternalStore } from "react";

/** The market's clock. Also the fallback before /api/status has answered, so a
 *  render that beats the settings load shows the default rather than the
 *  browser's zone — and never flips from one to the other mid-session. */
export const DEFAULT_ZONE = "America/New_York";

/** The short list offered in Settings. A novice does not want 400+ IANA names,
 *  and every one of these has a reason to be here: the three US market zones,
 *  UTC (what the database holds), the European desks, the owner's own zone, and
 *  the two Asia-Pacific ones. The browser's own zone is offered separately, at
 *  runtime, since it can be anything. */
export const ZONE_CHOICES: { zone: string; label: string }[] = [
  { zone: "America/New_York", label: "New York — US markets (ET)" },
  { zone: "America/Chicago", label: "Chicago (CT)" },
  { zone: "America/Los_Angeles", label: "Los Angeles (PT)" },
  { zone: "UTC", label: "UTC — what the database stores" },
  { zone: "Europe/London", label: "London" },
  { zone: "Europe/Amsterdam", label: "Amsterdam / Central Europe" },
  { zone: "Africa/Johannesburg", label: "Johannesburg" },
  { zone: "Asia/Tokyo", label: "Tokyo" },
  { zone: "Australia/Sydney", label: "Sydney" },
];

/** Whatever zone this browser thinks it is in — offered as an extra choice so
 *  "just show me my own time" doesn't require knowing your IANA name. */
export function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || DEFAULT_ZONE;
  } catch {
    return DEFAULT_ZONE;
  }
}

// ---------------------------------------------------------------------------
// The current zone: a module-level value, not a React context.
//
// Two reasons. Formatting is called from plain functions and from sort/map
// callbacks that are not components, where a hook is not available; and the
// zone arrives with /api/status, which App already blocks its first render on —
// so by the time any date is drawn the real value is in. The listener set is
// only for the one moment it CHANGES (a save in Settings), and App subscribes
// at the root so that re-renders the whole tree at once.
// ---------------------------------------------------------------------------

let zone = DEFAULT_ZONE;
const listeners = new Set<() => void>();

/** Reject a zone the runtime can't resolve rather than let every timestamp on
 *  the page throw a RangeError. A stale or hand-edited setting then degrades to
 *  the default instead of blanking the UI. */
function usable(tz: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz }).format(new Date());
    return true;
  } catch {
    return false;
  }
}

export function setDisplayZone(tz: string | null | undefined): void {
  const next = tz && usable(tz) ? tz : DEFAULT_ZONE;
  if (next === zone) return;
  zone = next;
  listeners.forEach((l) => l());
}

export function getDisplayZone(): string {
  return zone;
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

/** Subscribe a component to zone changes. App calls this at the root, which is
 *  all it takes for the whole tree to redraw when the setting is saved. */
export function useDisplayZone(): string {
  return useSyncExternalStore(subscribe, getDisplayZone, getDisplayZone);
}

/** The configured zone's short name for the moment being shown ("EDT", "SAST",
 *  "GMT+2"…). Column headers use it so a bare "18:15" says whose 18:15 it is —
 *  and it is derived, never hardcoded, so it follows the setting AND the DST
 *  side of the year. */
export function zoneAbbr(at?: Date): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      timeZoneName: "short",
    }).formatToParts(at ?? new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value ?? zone;
  } catch {
    return zone;
  }
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

/** Parse a server timestamp into an instant.
 *
 *  The API is UTC throughout, but not uniformly *stamped*: values that come back
 *  through SQLite have lost their tzinfo, so some endpoints emit "…T18:15:00"
 *  with no offset while others emit "…+00:00". A browser reads the offset-less
 *  form as LOCAL time — which is how UTC wall-clock times ended up on screen
 *  wearing the wrong label. Anything without a zone designator is UTC here,
 *  because that is what the database means by it. */
export function parseInstant(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const stamped = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso.replace(" ", "T")}Z`;
  const d = new Date(stamped);
  return Number.isNaN(d.getTime()) ? null : d;
}

const EMPTY = "—";

// ---------------------------------------------------------------------------
// Instants -> display. Each takes the ISO string the API returned.
// ---------------------------------------------------------------------------

/** Date and time, e.g. "8/2/2026, 6:15:00 PM" — the locale's own layout, but in
 *  the configured zone rather than the browser's. */
export function fmtDateTime(iso: string | null | undefined): string {
  const d = parseInstant(iso);
  return d ? d.toLocaleString(undefined, { timeZone: zone }) : EMPTY;
}

/** Just the calendar date of an instant, as that date falls in the configured
 *  zone. Note this is a real conversion: 00:30 UTC is still the previous day in
 *  New York, and that is the point. */
export function fmtDate(iso: string | null | undefined): string {
  const d = parseInstant(iso);
  return d ? d.toLocaleDateString(undefined, { timeZone: zone }) : EMPTY;
}

/** Calendar date of an instant, spelled out ("2 Aug 2026") — for chart readouts
 *  where a slash-separated date is easy to misread at a glance. */
export function fmtDateMed(iso: string | null | undefined): string {
  const d = parseInstant(iso);
  return d
    ? d.toLocaleDateString(undefined, {
        timeZone: zone,
        year: "numeric",
        month: "short",
        day: "numeric",
      })
    : EMPTY;
}

/** Just the clock time of an instant. */
export function fmtTime(iso: string | null | undefined): string {
  const d = parseInstant(iso);
  return d ? d.toLocaleTimeString(undefined, { timeZone: zone }) : EMPTY;
}

/** Clock time to the minute, e.g. "18:15" — for dense tables where seconds are
 *  noise. Forced 24-hour and fixed-width so a column of them stays a column. */
export function fmtHm(iso: string | null | undefined): string {
  const d = parseInstant(iso);
  if (!d) return EMPTY;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: zone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

/** "YYYY-MM-DD" for the instant, in the configured zone. Used where the old
 *  code sliced the ISO string — same shape (sortable, unambiguous, tabular),
 *  but the zone is now the user's instead of UTC. en-CA is simply the locale
 *  whose short date IS ISO order; nothing about it is Canadian. */
export function fmtIsoDate(iso: string | null | undefined): string {
  const d = parseInstant(iso);
  if (!d) return EMPTY;
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

/** Date and time as a Date object for a `Date` the app made itself (the "as of"
 *  stamp, which is a local clock reading rather than a server value). */
export function fmtClock(at: Date): string {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: zone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(at);
}

// ---------------------------------------------------------------------------
// Calendar days -> display. NOT instants.
// ---------------------------------------------------------------------------

/** Format a bare "YYYY-MM-DD" that the backend already bucketed to a calendar
 *  day (an ET trading day, or a UTC day for crypto).
 *
 *  These must NOT be converted. `new Date("2026-08-03")` is midnight UTC, and
 *  rendering that in New York moves it to the 2nd — the day would be reported
 *  wrong by one for most of the world's zones, on data that was never an
 *  instant to begin with. So: parse as UTC, format as UTC, and let only the
 *  locale's field order change. */
export function fmtCalendarDate(day: string | null | undefined): string {
  if (!day) return EMPTY;
  const d = new Date(`${day.slice(0, 10)}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? day : d.toLocaleDateString(undefined, { timeZone: "UTC" });
}

/** Sort key for a calendar day so it can be interleaved with real instants.
 *  UTC midnight: deterministic, and it lands before the trading hours of that
 *  same day in every market zone, which is the ordering these rows want. */
export function calendarDayMs(day: string): number {
  return Date.parse(`${day.slice(0, 10)}T00:00:00Z`);
}

/** Sort key for a server timestamp — the parse above, so an unstamped value is
 *  not silently shifted by the reader's own offset. */
export function instantMs(iso: string | null | undefined): number {
  return parseInstant(iso)?.getTime() ?? 0;
}
