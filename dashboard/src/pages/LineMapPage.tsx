import { useEffect, useRef, useState } from "react";

import type { LineMap, TwinScenarios } from "../api";
import { Card } from "../components/Card";
import { CountLegend, LineMapChart } from "../components/LineMapChart";
import { LineTag } from "../components/Legend";
import { ErrorText, Placeholder } from "../components/Status";
import { Table } from "../components/Table";
import { formatHour, formatInt } from "../format";
import { useApi } from "../hooks";

interface LineMapPageProps {
  at: number | null;
  lastHour: number;
  onTime: (at: number | null) => void;
}

const STEPS = [
  { hours: 1, label: "1 hour" },
  { hours: 6, label: "6 hours" },
  { hours: 24, label: "1 day" },
];
const FRAME_MS = 700;

export function LineMapPage({ at, lastHour, onTime }: LineMapPageProps) {
  const scenarios = useApi<TwinScenarios>("/twin/scenarios");
  const [source, setSource] = useState("real");
  const [compare, setCompare] = useState(true);
  const [playing, setPlaying] = useState(false);
  const [step, setStep] = useState(6);

  const hour = at ?? lastHour;
  const twin = scenarios.data?.scenarios.find((s) => s.id === source) ?? null;
  const inRange = !twin || (hour >= twin.first_hour && hour <= twin.last_hour);
  const showReal = !twin || compare;

  const real = useApi<LineMap>(showReal ? "/line/map" : null, { at_hour: at });
  const simulated = useApi<LineMap>(twin && inRange ? `/twin/map/${twin.id}` : null, { at_hour: hour });

  // Play: move the time control forward a step at a time
  const hourRef = useRef(hour);
  hourRef.current = hour;
  useEffect(() => {
    if (!playing) return;
    const end = twin ? twin.last_hour : lastHour;
    const timer = window.setInterval(() => {
      const next = Math.min(hourRef.current + step, end);
      onTime(next >= lastHour ? null : Math.round(next * 10) / 10);
      if (next >= end) setPlaying(false);
    }, FRAME_MS);
    return () => window.clearInterval(timer);
  }, [playing, step, twin, lastHour, onTime]);

  return (
    <div className="page">
      <div className="map-controls" role="group" aria-label="Line map controls">
        <label>
          Show
          <select value={source} onChange={(event) => setSource(event.target.value)}>
            <option value="real">The real line</option>
            {scenarios.data?.scenarios.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        {twin && (
          <label className="check">
            <input type="checkbox" checked={compare} onChange={(event) => setCompare(event.target.checked)} />
            Compare with the real line
          </label>
        )}
        <div className="play">
          <button type="button" className="primary" onClick={() => setPlaying((p) => !p)} disabled={!inRange}>
            {playing ? "Pause" : "Play"}
          </button>
          <label>
            Step
            <select value={step} onChange={(event) => setStep(Number(event.target.value))}>
              {STEPS.map((s) => (
                <option key={s.hours} value={s.hours}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {twin && <p className="muted small">{twin.description}. {scenarios.data?.note}</p>}

      {twin && !inRange && (
        <div className="notice">
          <p>
            <strong>
              This twin run covers hours {formatHour(twin.first_hour)}–{formatHour(twin.last_hour)} (weeks{" "}
              {Math.floor(twin.first_hour / 168)}–{Math.floor(twin.last_hour / 168)}).
            </strong>
          </p>
          <button type="button" onClick={() => onTime(70 * 168)}>
            Go to week 70
          </button>
        </div>
      )}

      {showReal && <MapCard title={twin ? "The real line" : "Where the parts are"} loaded={real} />}
      {twin && inRange && <MapCard title={twin.label} loaded={simulated} />}
    </div>
  );
}

function MapCard({ title, loaded }: { title: string; loaded: { data?: LineMap; error?: Error; loading: boolean } }) {
  const data = loaded.data;

  return (
    <Card
      title={title}
      subtitle={data ? `Hour ${formatHour(data.at_hour)} · ${formatInt(data.in_production)} parts in production` : undefined}
      loading={loaded.loading}
      note={data?.note}
      table={data && <MapTable data={data} />}
    >
      {loaded.error ? (
        <ErrorText error={loaded.error} />
      ) : data ? (
        <>
          <div className="map-stats">
            <span>
              Entered in the last hour: <LineTag line="L0">L0 {formatInt(data.entered_last_hour.L0)}</LineTag>{" "}
              <LineTag line="L1">L1 {formatInt(data.entered_last_hour.L1)}</LineTag>
            </span>
            <span>QC results in the last 24 h: {formatInt(data.qc_reported_last_24h)}</span>
          </div>
          <div className="map-scroll">
            <LineMapChart data={data} label={title} />
          </div>
          <CountLegend />
        </>
      ) : (
        <Placeholder height={300} />
      )}
    </Card>
  );
}

function MapTable({ data }: { data: LineMap }) {
  const rows = [
    ...data.stations.map((s) => ({ key: s.station, place: s.station, line: s.line, parts: s.parts })),
    { key: "queue", place: "Waiting for line 3", line: "", parts: data.waiting_for_line3.total },
  ];
  return (
    <Table
      caption="Parts in production by position"
      rows={rows}
      rowKey={(row) => row.key}
      columns={[
        { key: "place", label: "Position", render: (row) => (row.line ? <LineTag line={row.line}>{row.place}</LineTag> : row.place) },
        { key: "parts", label: "Parts", numeric: true, render: (row) => formatInt(row.parts) },
      ]}
    />
  );
}
