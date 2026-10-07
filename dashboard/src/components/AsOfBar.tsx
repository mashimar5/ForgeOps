import { useEffect, useState } from "react";

import { formatHour, weekAndDay } from "../format";

interface AsOfBarProps {
  /** null = the end of the data ("now") */
  at: number | null;
  lastHour: number;
  trainingCutoff: number;
  onChange: (at: number | null) => void;
}

const STEPS = [
  { label: "−1 week", hours: -168 },
  { label: "−1 day", hours: -24 },
  { label: "+1 day", hours: 24 },
  { label: "+1 week", hours: 168 },
];

/**
 * The one control every page answers to: the production hour to show the
 * factory as of. Pages only ever see what was known at that hour.
 */
export function AsOfBar({ at, lastHour, trainingCutoff, onChange }: AsOfBarProps) {
  const current = at ?? lastHour;
  const [draft, setDraft] = useState(current);
  const [typed, setTyped] = useState(current.toFixed(1));

  // Follow changes made elsewhere (step buttons, links, back and forward)
  useEffect(() => {
    setDraft(current);
    setTyped(current.toFixed(1));
  }, [current]);

  // Commit the slider once it rests, so dragging doesn't flood the API
  useEffect(() => {
    if (draft === current) return;
    const timer = window.setTimeout(() => commit(draft), 250);
    return () => window.clearTimeout(timer);
  }, [draft]);

  function commit(hour: number) {
    const clamped = Math.min(Math.max(Math.round(hour * 10) / 10, 0), lastHour);
    onChange(clamped >= lastHour ? null : clamped);
  }

  function submitTyped(event?: { preventDefault: () => void }) {
    event?.preventDefault();
    const hour = Number(typed);
    if (typed.trim() !== "" && Number.isFinite(hour)) commit(hour);
    else setTyped(current.toFixed(1));
  }

  const { week, day } = weekAndDay(draft);

  return (
    <div className="asof" role="group" aria-label="Time">
      <div className="asof-row">
        <form className="asof-hour" onSubmit={submitTyped}>
          <label htmlFor="asof-hour">As of hour</label>
          <input
            id="asof-hour"
            inputMode="decimal"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            onBlur={submitTyped}
          />
        </form>
        <span className="asof-context">
          Week {week}, day {day}
          {at === null ? " · end of the data" : ""}
        </span>
        <div className="asof-steps">
          {STEPS.map((step) => (
            <button
              key={step.label}
              type="button"
              onClick={() => commit(current + step.hours)}
              disabled={step.hours < 0 ? current <= 0 : at === null}
            >
              {step.label}
            </button>
          ))}
          <button type="button" className="primary" onClick={() => onChange(null)} disabled={at === null}>
            Now
          </button>
        </div>
      </div>
      <input
        className="asof-range"
        type="range"
        min={0}
        max={lastHour}
        step={0.1}
        value={draft}
        onChange={(event) => setDraft(Number(event.target.value))}
        aria-label="As of production hour"
        aria-valuetext={`Hour ${formatHour(draft)}, week ${week}, day ${day}`}
      />
      {draft <= trainingCutoff && (
        <p className="asof-hint">
          Before hour {formatHour(trainingCutoff)} the model has no risk scores: it trained on the parts that finished
          by then.
        </p>
      )}
    </div>
  );
}
