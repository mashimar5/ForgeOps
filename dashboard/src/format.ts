const integer = new Intl.NumberFormat("en-US");
const oneDecimal = new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export const formatInt = (value: number) => integer.format(value);
export const formatCompact = (value: number) => compact.format(value);

/** Production hours, e.g. 17,184.8 */
export const formatHour = (value: number) => oneDecimal.format(value);

export function formatPct(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "–" : `${value.toFixed(digits)}%`;
}

export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : value.toFixed(3);
}

/** Axis ticks from 0 in round steps (1, 2 or 5 x 10^k), about `count` of them past zero */
export function niceTicks(max: number, count = 4): number[] {
  if (!(max > 0)) return [0, 1];
  const rough = max / count;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 5, 10].map((m) => m * power).find((s) => s >= rough) ?? 10 * power;
  const steps = Math.ceil(max / step - 1e-9);
  return Array.from({ length: steps + 1 }, (_, i) => Number((i * step).toPrecision(12)));
}

/** Week w starts at hour 168 * w; days count from 1. */
export function weekAndDay(hour: number): { week: number; day: number } {
  const week = Math.floor(hour / 168);
  return { week, day: Math.floor((hour - week * 168) / 24) + 1 };
}

/** "L3_S32" -> 32 */
export function stationNumber(station: string): number {
  return Number(station.split("_S")[1]);
}

/** "L3_S32" -> "L3" */
export function lineOf(station: string): string {
  return station.split("_")[0];
}
