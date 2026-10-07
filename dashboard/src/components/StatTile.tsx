import type { ReactNode } from "react";

interface StatTileProps {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  /** The one number a view leads with */
  hero?: boolean;
}

export function StatTile({ label, value, detail, hero = false }: StatTileProps) {
  return (
    <div className={hero ? "tile tile-hero" : "tile"}>
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {detail && <div className="tile-detail">{detail}</div>}
    </div>
  );
}
