import type { ReactNode } from "react";

import { lineColor, useColors } from "../theme";

export type Mark = "rect" | "line" | "dot";

export interface LegendItem {
  label: string;
  color: string;
  mark?: Mark;
}

/** Legend keys mirror the mark: rect for bars, line for lines, dot for points */
export function Legend({ items, mark = "rect" }: { items: LegendItem[]; mark?: Mark }) {
  return (
    <ul className="legend">
      {items.map((item) => (
        <li key={item.label}>
          <span className={`legend-mark legend-${item.mark ?? mark}`} style={{ background: item.color }} aria-hidden="true" />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

export function LineLegend({ lines, mark, extra = [] }: { lines: readonly string[]; mark: Mark; extra?: LegendItem[] }) {
  const colors = useColors();
  return <Legend mark={mark} items={[...lines.map((line) => ({ label: line, color: lineColor(colors, line) })), ...extra]} />;
}

/** A line's color beside a name, e.g. the line or one of its stations (the text stays in ink) */
export function LineTag({ line, children }: { line: string; children?: ReactNode }) {
  const colors = useColors();
  return (
    <span className="line-tag">
      <span className="legend-mark legend-dot" style={{ background: lineColor(colors, line) }} aria-hidden="true" />
      {children ?? line}
    </span>
  );
}
