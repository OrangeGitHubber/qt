// Pure technical-indicator helpers for the symbol-detail review chart.
//
// These are DISPLAY-ONLY: they annotate a chart so you can eyeball what a
// strategy's signals were doing. They are deliberately independent of the
// engine's own look-ahead-safe stats (backend qt/services/stats.py) — nothing
// here ever feeds a trading decision, so a simpler implementation is fine.
//
// Every function returns an array the SAME LENGTH as its input, with `null` in
// the leading positions where there isn't enough history yet. That lets the
// chart align an indicator to its price bar by index without any offset math.

export type Num = number | null;

/** Simple moving average over the trailing `period` closes. */
export function sma(values: number[], period: number): Num[] {
  const out: Num[] = new Array(values.length).fill(null);
  if (period < 1) return out;
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

/** Exponential moving average. Seeded with the SMA of the first `period` values
 *  (the standard seed), then EMA_i = c_i·k + EMA_{i-1}·(1−k), k = 2/(period+1). */
export function ema(values: number[], period: number): Num[] {
  const out: Num[] = new Array(values.length).fill(null);
  if (period < 1 || values.length < period) return out;
  const k = 2 / (period + 1);
  let seed = 0;
  for (let i = 0; i < period; i++) seed += values[i];
  let prev = seed / period;
  out[period - 1] = prev;
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

export interface MacdSeries {
  macd: Num[];
  signal: Num[];
  hist: Num[];
}

/** MACD line (EMA_fast − EMA_slow), its signal EMA, and the histogram between
 *  them. Standard defaults 12/26/9. The signal EMA is computed only over the
 *  positions where the MACD line exists, then re-aligned to the full length. */
export function macd(closes: number[], fast = 12, slow = 26, signal = 9): MacdSeries {
  const ef = ema(closes, fast);
  const es = ema(closes, slow);
  const line: Num[] = closes.map((_, i) => (ef[i] != null && es[i] != null ? (ef[i] as number) - (es[i] as number) : null));

  // Signal EMA over the contiguous non-null tail of the MACD line.
  const firstIdx = line.findIndex((v) => v != null);
  const sig: Num[] = new Array(closes.length).fill(null);
  const hist: Num[] = new Array(closes.length).fill(null);
  if (firstIdx !== -1) {
    const compact = line.slice(firstIdx).map((v) => v as number);
    const sigCompact = ema(compact, signal);
    for (let j = 0; j < sigCompact.length; j++) {
      const i = firstIdx + j;
      sig[i] = sigCompact[j];
      if (sig[i] != null && line[i] != null) hist[i] = (line[i] as number) - (sig[i] as number);
    }
  }
  return { macd: line, signal: sig, hist };
}

/** Wilder's RSI over `period` (default 14): 100 − 100/(1+RS), RS = avgGain/avgLoss.
 *  Seeded with a simple average of the first `period` changes, then smoothed. */
export function rsi(closes: number[], period = 14): Num[] {
  const out: Num[] = new Array(closes.length).fill(null);
  if (closes.length <= period) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d >= 0) gain += d;
    else loss -= d;
  }
  let avgG = gain / period;
  let avgL = loss / period;
  out[period] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL);
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    const g = d >= 0 ? d : 0;
    const l = d < 0 ? -d : 0;
    avgG = (avgG * (period - 1) + g) / period;
    avgL = (avgL * (period - 1) + l) / period;
    out[i] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL);
  }
  return out;
}

export interface Bollinger {
  mid: Num[];
  upper: Num[];
  lower: Num[];
}

/** Bollinger Bands: a `period`-SMA middle band with upper/lower bands `mult`
 *  population standard deviations away (defaults 20, 2). */
export function bollinger(closes: number[], period = 20, mult = 2): Bollinger {
  const mid = sma(closes, period);
  const upper: Num[] = new Array(closes.length).fill(null);
  const lower: Num[] = new Array(closes.length).fill(null);
  for (let i = period - 1; i < closes.length; i++) {
    const m = mid[i] as number;
    let variance = 0;
    for (let j = i - period + 1; j <= i; j++) variance += (closes[j] - m) ** 2;
    const sd = Math.sqrt(variance / period);
    upper[i] = m + mult * sd;
    lower[i] = m - mult * sd;
  }
  return { mid, upper, lower };
}

export interface OHLC {
  c: number;
  h?: number | null;
  l?: number | null;
}

/** Wilder's ATR (Average True Range) in PRICE terms over `period` (default 14).
 *  True range = max(high−low, |high−prevClose|, |low−prevClose|); when a bar has
 *  no high/low we fall back to |close−prevClose| so the series still forms. */
export function atr(bars: OHLC[], period = 14): Num[] {
  const out: Num[] = new Array(bars.length).fill(null);
  if (bars.length <= period) return out;
  const tr: number[] = new Array(bars.length).fill(0);
  for (let i = 1; i < bars.length; i++) {
    const prevC = bars[i - 1].c;
    const h = bars[i].h ?? bars[i].c;
    const l = bars[i].l ?? bars[i].c;
    tr[i] = Math.max(h - l, Math.abs(h - prevC), Math.abs(l - prevC));
  }
  let sum = 0;
  for (let i = 1; i <= period; i++) sum += tr[i];
  let prev = sum / period;
  out[period] = prev;
  for (let i = period + 1; i < bars.length; i++) {
    prev = (prev * (period - 1) + tr[i]) / period;
    out[i] = prev;
  }
  return out;
}

/** A volatility-based stop LEVEL under each bar: close − mult·ATR (price terms).
 *  Illustrative — it shows how far a `mult`×ATR stop sits below price and how it
 *  breathes with volatility; it is NOT a specific trade's live stop. */
export function atrStopLevel(bars: OHLC[], period = 14, mult = 2): Num[] {
  const a = atr(bars, period);
  return bars.map((b, i) => (a[i] != null ? b.c - mult * (a[i] as number) : null));
}

/** Relative strength vs a benchmark, rebased to 1.0 at the window's first shared
 *  point: (sym_i / sym_0) / (bench_i / bench_0). Above 1 = outperforming since
 *  the start of the window; the slope is what matters. `bench` is aligned to the
 *  same index as `sym` (caller matches by date first). */
export function relativeStrength(sym: number[], bench: Num[]): Num[] {
  const s0 = sym[0];
  const b0 = bench.find((v) => v != null) as number | undefined;
  if (!s0 || !b0) return new Array(sym.length).fill(null);
  return sym.map((s, i) => {
    const b = bench[i];
    if (b == null || b === 0) return null;
    return s / s0 / (b / b0);
  });
}
