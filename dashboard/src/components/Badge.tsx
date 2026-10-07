import type { ReactNode } from "react";

export type Tone = "good" | "warning" | "critical" | "neutral";

// A status color never carries meaning alone: each badge pairs an icon with a label
const ICONS: Record<Tone, ReactNode> = {
  good: <path d="M3.5 8.5l3 3 6-7" />,
  warning: (
    <>
      <path d="M8 2.5l6 11H2z" />
      <path d="M8 7v2.5M8 11.5v.01" />
    </>
  ),
  critical: (
    <>
      <circle cx="8" cy="8" r="6" />
      <path d="M8 5v3.5M8 11v.01" />
    </>
  ),
  neutral: <circle cx="8" cy="8" r="3" />,
};

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`badge badge-${tone}`}>
      <svg viewBox="0 0 16 16" className="badge-icon" aria-hidden="true">
        {ICONS[tone]}
      </svg>
      {children}
    </span>
  );
}
