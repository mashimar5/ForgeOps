import type { ReactNode } from "react";

export interface TooltipRow {
  label: string;
  value: ReactNode;
  /** A short stroke in the series color keys the row */
  color?: string;
}

/** Chart tooltip: the value leads, the label follows */
export function TooltipBox({ title, rows }: { title: ReactNode; rows: TooltipRow[] }) {
  return (
    <div className="tooltip">
      <div className="tooltip-title">{title}</div>
      {rows.map((row) => (
        <div className="tooltip-row" key={row.label}>
          <span className="tooltip-key" style={{ background: row.color ?? "transparent" }} aria-hidden="true" />
          <strong>{row.value}</strong>
          <span>{row.label}</span>
        </div>
      ))}
    </div>
  );
}

/** What Recharts passes a custom tooltip */
export interface TipProps {
  active?: boolean;
  payload?: ReadonlyArray<{ payload?: unknown }>;
}

export function tipPoint<T>({ active, payload }: TipProps): T | undefined {
  return active ? (payload?.[0]?.payload as T | undefined) : undefined;
}
