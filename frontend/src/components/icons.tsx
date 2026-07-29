import type { ComponentType } from "react";
import {
  Play,
  Pause,
  Pencil,
  Trash2,
  X,
  AlertTriangle,
  PauseCircle,
  type LucideProps,
} from "lucide-react";

// Single control point for the app's action/control icons. Size and stroke are
// locked here so icons can't drift across call sites, and everything comes from
// one family (Lucide). Lucide draws with `currentColor`, so each icon inherits
// its button/text colour automatically — never hardcode a colour at a call site.
const base =
  (Icon: ComponentType<LucideProps>) =>
  (p: LucideProps) =>
    <Icon size={16} strokeWidth={1.75} aria-hidden {...p} />;

export const IconPlay = base(Play); // Enable / start
export const IconPause = base(Pause); // Pause
export const IconEdit = base(Pencil); // Edit
export const IconDelete = base(Trash2); // Delete
export const IconClose = base(X); // Modal / dialog close
export const IconWarn = base(AlertTriangle); // Inline warning marker
export const IconMarketClosed = base(PauseCircle); // Market-closed indicator
