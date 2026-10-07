import { useState, type ReactNode } from "react";

interface CardProps {
  title: string;
  subtitle?: ReactNode;
  /** Caveats under the content */
  note?: ReactNode;
  /** The chart's table view: every value a chart shows, readable without hovering */
  table?: ReactNode;
  /** Data is reloading: the previous content stays, dimmed */
  loading?: boolean;
  className?: string;
  children: ReactNode;
}

export function Card({ title, subtitle, note, table, loading = false, className = "", children }: CardProps) {
  const [showTable, setShowTable] = useState(false);

  return (
    <section className={`card ${loading ? "is-loading" : ""} ${className}`.trim()} aria-busy={loading}>
      <header className="card-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p className="card-sub">{subtitle}</p>}
        </div>
        {table && (
          <button type="button" className="toggle" aria-pressed={showTable} onClick={() => setShowTable((shown) => !shown)}>
            {showTable ? "Show chart" : "Show table"}
          </button>
        )}
      </header>
      <div className="card-body">{showTable && table ? table : children}</div>
      {note && <p className="card-note">{note}</p>}
    </section>
  );
}
