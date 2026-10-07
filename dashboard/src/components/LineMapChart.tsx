import type { LineMap, MapStation } from "../api";
import { formatCompact, formatInt } from "../format";
import { COUNT_BINS, countStep, lineColor, useColors } from "../theme";
import { Legend } from "./Legend";

// Schematic in production order: one row per line, line 3 after the queue
const BOX = { small: 34, wide: 92, height: 30, gap: 6 };
const LEFT = 64;
const ROWS = [
  { line: "L0", from: 0, to: 23, y: 26, width: BOX.small },
  { line: "L1", from: 24, to: 25, y: 104, width: BOX.wide },
  { line: "L2", from: 26, to: 28, y: 168, width: BOX.wide },
  { line: "L3", from: 29, to: 51, y: 296, width: BOX.small },
];
const QUEUE = { x: 430, y: 96, width: 470, height: 128 };
const QC = { x: LEFT + 23 * (BOX.small + BOX.gap) + 14, y: 290, width: 120, height: 64 };
const WIDTH = 1120;
const HEIGHT = 380;

function stationNumberOf(station: MapStation): number {
  return Number(station.station.split("_S")[1]);
}

/** Where the parts in production are: a box per station, the queue for line 3, final QC */
export function LineMapChart({ data, label }: { data: LineMap; label: string }) {
  const colors = useColors();
  const byNumber = new Map(data.stations.map((s) => [stationNumberOf(s), s]));
  const queue = data.waiting_for_line3;
  const serving = data.line3_serving;

  return (
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="line-map" role="img" aria-label={`${label}: where the parts in production are`}>
      {/* Flow: upstream rows into the queue, the queue into line 3 */}
      <g stroke={colors.axis} strokeWidth={1.5} fill="none" markerEnd="url(#arrow)">
        <path d={`M ${LEFT + 24 * (BOX.small + BOX.gap) - BOX.gap + 6} ${26 + BOX.height / 2} h 30 V ${QUEUE.y + 20} H ${QUEUE.x + QUEUE.width + 8}`} />
        <path d={`M ${LEFT + 2 * (BOX.wide + BOX.gap) + 4} ${104 + BOX.height / 2} H ${QUEUE.x - 6}`} />
        <path d={`M ${LEFT + 3 * (BOX.wide + BOX.gap) + 4} ${168 + BOX.height / 2} H ${QUEUE.x - 6}`} />
        <path d={`M ${QUEUE.x + 40} ${QUEUE.y + QUEUE.height + 2} V 270 H ${LEFT + BOX.small / 2} V ${296 - 6}`} />
      </g>
      <defs>
        <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L8 4 L0 8 z" fill={colors.axis} />
        </marker>
      </defs>

      {ROWS.map((row) => (
        <g key={row.line}>
          <circle cx={14} cy={row.y + BOX.height / 2} r={5} fill={lineColor(colors, row.line)} />
          <text x={26} y={row.y + BOX.height / 2} dominantBaseline="central" fontSize={14} fontWeight={600} fill={colors.ink}>
            {row.line}
          </text>
          {Array.from({ length: row.to - row.from + 1 }, (_, i) => {
            const station = byNumber.get(row.from + i);
            if (!station) return null;
            const x = LEFT + i * (row.width + BOX.gap);
            return <StationBox key={station.station} station={station} x={x} y={row.y} width={row.width} />;
          })}
        </g>
      ))}

      {/* Line 3's status beside its row */}
      <text x={LEFT + 40} y={286} fontSize={12} fill={colors.ink2}>
        {serving === "off" ? "Line 3 is off shift" : `Line 3 is serving ${serving === "other" ? "L2/L3-entry" : serving} parts`}
        {" · "}
        {formatInt(Object.values(data.line3_started_last_hour).reduce((a, b) => a + b, 0))} started in the last hour
      </text>

      <QueueBox queue={queue} />

      {/* Final QC */}
      <g>
        <rect x={QC.x} y={QC.y} width={QC.width} height={QC.height} rx={6} fill="none" stroke={colors.axis} />
        <text x={QC.x + 10} y={QC.y + 18} fontSize={12} fontWeight={600} fill={colors.ink}>
          Final QC
        </text>
        <text x={QC.x + 10} y={QC.y + 36} fontSize={11} fill={colors.ink2}>
          {formatInt(data.finished_last_hour)} done in last hour
        </text>
        <text x={QC.x + 10} y={QC.y + 52} fontSize={11} fill={colors.ink2}>
          {formatInt(data.qc_failed_last_24h)} failed in last 24 h
        </text>
      </g>
    </svg>
  );
}

function StationBox({ station, x, y, width }: { station: MapStation; x: number; y: number; width: number }) {
  const colors = useColors();
  const step = countStep(station.parts);
  const fill = step < 0 ? colors.empty : colors.ramp[step];
  const short = station.station.split("_")[1];
  const text = station.parts === 0 ? "" : width > BOX.small ? formatInt(station.parts) : formatCompact(station.parts);

  return (
    <g>
      <title>{`${station.station}: ${formatInt(station.parts)} part${station.parts === 1 ? "" : "s"}`}</title>
      <rect x={x} y={y} width={width} height={BOX.height} rx={4} fill={fill} stroke={step < 0 ? colors.axis : "none"} strokeWidth={step < 0 ? 0.5 : 0} />
      {text && (
        <text x={x + width / 2} y={y + BOX.height / 2} textAnchor="middle" dominantBaseline="central" fontSize={10.5}
          fontWeight={600} fill={colors.rampText[step]} style={{ fontVariantNumeric: "tabular-nums" }}>
          {text}
        </text>
      )}
      <text x={x + width / 2} y={y + BOX.height + 12} textAnchor="middle" fontSize={9.5} fill={colors.muted}>
        {short}
      </text>
    </g>
  );
}

function QueueBox({ queue }: { queue: LineMap["waiting_for_line3"] }) {
  const colors = useColors();
  const parts = [
    { key: "L0", label: "from L0", value: queue.L0, color: lineColor(colors, "L0") },
    { key: "L1", label: "from L1", value: queue.L1, color: lineColor(colors, "L1") },
    { key: "other", label: "from L2/L3", value: queue.other, color: colors.muted },
  ];
  const barX = QUEUE.x + 16;
  const barWidth = QUEUE.width - 32;
  let offset = 0;

  return (
    <g>
      <title>{`Waiting for line 3: ${formatInt(queue.total)} parts (L0 ${formatInt(queue.L0)}, L1 ${formatInt(queue.L1)})`}</title>
      <rect x={QUEUE.x} y={QUEUE.y} width={QUEUE.width} height={QUEUE.height} rx={8} fill="none" stroke={colors.axis} strokeWidth={1.5} />
      <text x={QUEUE.x + 16} y={QUEUE.y + 24} fontSize={13} fontWeight={600} fill={colors.ink}>
        Waiting for line 3
      </text>
      <text x={QUEUE.x + 16} y={QUEUE.y + 60} fontSize={30} fontWeight={600} fill={colors.ink}>
        {formatInt(queue.total)}
      </text>
      <text x={QUEUE.x + 16 + String(formatInt(queue.total)).length * 18 + 10} y={QUEUE.y + 60} fontSize={12} fill={colors.ink2}>
        parts
      </text>
      {queue.total > 0 &&
        parts.map((part) => {
          const w = (part.value / queue.total) * barWidth;
          const rect = w > 0 ? <rect key={part.key} x={barX + offset} y={QUEUE.y + 76} width={Math.max(w - 2, 0)} height={14} rx={2} fill={part.color} /> : null;
          offset += w;
          return rect;
        })}
      {parts.map((part, i) => (
        <text key={part.key} x={barX + i * 150} y={QUEUE.y + 110} fontSize={11.5} fill={colors.ink2}>
          {`${part.label}: ${formatInt(part.value)}`}
        </text>
      ))}
    </g>
  );
}

/** The count bands, for a legend under the map */
export function CountLegend() {
  const colors = useColors();
  const items = COUNT_BINS.map((lower, i) => ({
    label: i + 1 < COUNT_BINS.length ? `${formatInt(lower)}–${formatInt(COUNT_BINS[i + 1] - 1)}` : `${formatInt(lower)}+`,
    color: colors.ramp[i],
  }));
  return <Legend mark="rect" items={[{ label: "no parts", color: colors.empty }, ...items]} />;
}
