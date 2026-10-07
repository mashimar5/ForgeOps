import { Bar, BarChart, CartesianGrid, Cell, ReferenceDot, ReferenceLine, Tooltip, XAxis, YAxis } from "recharts";

import type { StationMetrics } from "../api";
import { formatHour, formatInt, formatPct, niceTicks } from "../format";
import { axisTick, lineColor, useColors } from "../theme";
import { TooltipBox, tipPoint, type TipProps } from "./TooltipBox";

/** QC failure rate of the parts that visited each station, in production order */
export function StationChart({ stations, overallPct }: { stations: StationMetrics[]; overallPct: number | null }) {
  const colors = useColors();

  // The one direct label: the highest bar
  const top = stations.reduce<StationMetrics | null>(
    (best, station) =>
      station.failure_rate_pct !== null && (best === null || station.failure_rate_pct > (best.failure_rate_pct ?? -1))
        ? station
        : best,
    null,
  );
  const ticks = niceTicks(Math.max(top?.failure_rate_pct ?? 0, overallPct ?? 0));

  return (
    <BarChart
      data={stations}
      responsive
      width="100%"
      height={300}
      barCategoryGap={2}
      margin={{ top: 24, right: 12, bottom: 0, left: 0 }}
    >
      <CartesianGrid vertical={false} stroke={colors.grid} />
      <XAxis
        dataKey="station"
        tick={axisTick(colors)}
        tickLine={false}
        axisLine={{ stroke: colors.axis }}
        tickFormatter={(station: string) => station.split("_")[1]}
        minTickGap={8}
      />
      <YAxis
        tick={axisTick(colors)}
        tickLine={false}
        axisLine={false}
        width={48}
        domain={[0, ticks[ticks.length - 1]]}
        ticks={ticks}
        tickFormatter={(value: number) => `${value}%`}
      />
      {overallPct !== null && <ReferenceLine y={overallPct} stroke={colors.ink2} strokeWidth={1} />}
      <Tooltip
        isAnimationActive={false}
        cursor={{ fill: colors.grid, fillOpacity: 0.5 }}
        content={(props) => <StationTip active={props.active} payload={props.payload} />}
      />
      <Bar dataKey="failure_rate_pct" radius={[4, 4, 0, 0]} maxBarSize={24} isAnimationActive={false}>
        {stations.map((station) => (
          <Cell key={station.station} fill={lineColor(colors, station.line)} />
        ))}
      </Bar>
      {top && top.failure_rate_pct !== null && (
        <ReferenceDot
          x={top.station}
          y={top.failure_rate_pct}
          r={0}
          label={{
            value: `${top.station} ${top.failure_rate_pct.toFixed(2)}%`,
            position: "top",
            fill: colors.ink2,
            fontSize: 12,
          }}
        />
      )}
    </BarChart>
  );
}

function StationTip(props: TipProps) {
  const colors = useColors();
  const station = tipPoint<StationMetrics>(props);
  if (!station) return null;

  return (
    <TooltipBox
      title={station.station}
      rows={[
        { label: "QC failure rate", value: formatPct(station.failure_rate_pct, 3), color: lineColor(colors, station.line) },
        { label: "risk lift (vs all parts)", value: station.risk_lift === null ? "–" : `${station.risk_lift.toFixed(2)}×` },
        { label: "parts visited", value: formatInt(station.parts_visited) },
        { label: "QC results known", value: formatInt(station.qc_results_known) },
        {
          label: "median hours after entry",
          value: station.median_hours_after_entry === null ? "–" : formatHour(station.median_hours_after_entry),
        },
      ]}
    />
  );
}
