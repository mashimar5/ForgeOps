import { Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, Tooltip, XAxis, YAxis } from "recharts";

import type { Week } from "../api";
import { formatCompact, formatHour, formatInt, formatPct, niceTicks } from "../format";
import { LINES, axisTick, useColors } from "../theme";
import { Table } from "./Table";
import { TooltipBox, tipPoint, type TipProps } from "./TooltipBox";

// A weekly rate from fewer results is noise: the production break in weeks
// 50-51 has none, and a week's first hours only a few hundred
export const MIN_WEEKLY_RESULTS = 500;

const HEIGHT = 240;

function weekTitle(week: Week): string {
  const end = week.start_hour + week.hours_covered;
  const partial = week.hours_covered < 168 ? " (so far)" : "";
  return `Week ${week.week} · hours ${formatHour(week.start_hour)}–${formatHour(end)}${partial}`;
}

// ============================================================
// QC FAILURE RATE BY WEEK
// ============================================================

interface RatePoint extends Week {
  rate: number | null;
}

export function WeeklyRateChart({ weeks, overallPct }: { weeks: Week[]; overallPct: number | null }) {
  const colors = useColors();
  const data: RatePoint[] = weeks.map((week) => ({
    ...week,
    rate: week.qc_results >= MIN_WEEKLY_RESULTS ? week.qc_failure_rate_pct : null,
  }));
  const ticks = niceTicks(Math.max(...data.map((week) => week.rate ?? 0), overallPct ?? 0));

  return (
    <LineChart data={data} responsive width="100%" height={HEIGHT} margin={{ top: 16, right: 12, bottom: 0, left: 0 }}>
      <CartesianGrid vertical={false} stroke={colors.grid} />
      <XAxis dataKey="week" tick={axisTick(colors)} tickLine={false} axisLine={{ stroke: colors.axis }} interval={9} />
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
        cursor={{ stroke: colors.axis, strokeWidth: 1 }}
        content={(props) => <RateTip active={props.active} payload={props.payload} />}
      />
      <Line
        dataKey="rate"
        stroke={colors.total}
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
        dot={false}
        activeDot={{ r: 4, fill: colors.total, stroke: colors.surface, strokeWidth: 2 }}
        isAnimationActive={false}
      />
    </LineChart>
  );
}

function RateTip(props: TipProps) {
  const colors = useColors();
  const week = tipPoint<RatePoint>(props);
  if (!week) return null;

  return (
    <TooltipBox
      title={weekTitle(week)}
      rows={[
        {
          label: "QC failure rate",
          value: week.rate === null ? "too few results" : formatPct(week.rate, 2),
          color: colors.total,
        },
        { label: "QC results reported", value: formatInt(week.qc_results) },
        { label: "failed", value: formatInt(week.qc_failures) },
      ]}
    />
  );
}

// ============================================================
// PARTS ENTERING PRODUCTION BY WEEK, BY ENTRY LINE
// ============================================================

export function WeeklyEntriesChart({ weeks }: { weeks: Week[] }) {
  const colors = useColors();
  const data = weeks.map((week) => ({ ...week, ...week.parts_entered }));
  const ticks = niceTicks(Math.max(...weeks.map((week) => LINES.reduce((sum, line) => sum + (week.parts_entered[line] ?? 0), 0))));

  return (
    <BarChart
      data={data}
      responsive
      width="100%"
      height={HEIGHT}
      barCategoryGap={1}
      margin={{ top: 16, right: 12, bottom: 0, left: 0 }}
    >
      <CartesianGrid vertical={false} stroke={colors.grid} />
      <XAxis dataKey="week" tick={axisTick(colors)} tickLine={false} axisLine={{ stroke: colors.axis }} interval={9} />
      <YAxis
        tick={axisTick(colors)}
        tickLine={false}
        axisLine={false}
        width={48}
        domain={[0, ticks[ticks.length - 1]]}
        ticks={ticks}
        tickFormatter={formatCompact}
      />
      <Tooltip
        isAnimationActive={false}
        cursor={{ fill: colors.grid, fillOpacity: 0.5 }}
        content={(props) => <EntriesTip active={props.active} payload={props.payload} />}
      />
      {LINES.map((line) => (
        <Bar key={line} dataKey={line} stackId="lines" fill={colors.lines[line]} isAnimationActive={false} />
      ))}
    </BarChart>
  );
}

function EntriesTip(props: TipProps) {
  const colors = useColors();
  const week = tipPoint<Week>(props);
  if (!week) return null;

  const total = LINES.reduce((sum, line) => sum + (week.parts_entered[line] ?? 0), 0);

  return (
    <TooltipBox
      title={weekTitle(week)}
      rows={[
        ...LINES.map((line) => ({ label: line, value: formatInt(week.parts_entered[line] ?? 0), color: colors.lines[line] })),
        { label: "parts entered", value: formatInt(total) },
      ]}
    />
  );
}

// ============================================================
// TABLE VIEW FOR BOTH CHARTS
// ============================================================

export function WeeklyTable({ weeks }: { weeks: Week[] }) {
  return (
    <Table
      caption="QC results and parts entered, by week"
      rows={[...weeks].reverse()}
      rowKey={(week) => week.week}
      columns={[
        { key: "week", label: "Week", numeric: true, render: (week) => week.week },
        { key: "start", label: "Starts at hour", numeric: true, render: (week) => formatHour(week.start_hour) },
        { key: "results", label: "QC results", numeric: true, render: (week) => formatInt(week.qc_results) },
        { key: "failed", label: "Failed", numeric: true, render: (week) => formatInt(week.qc_failures) },
        { key: "rate", label: "Failure rate", numeric: true, render: (week) => formatPct(week.qc_failure_rate_pct, 2) },
        ...LINES.map((line) => ({
          key: line,
          label: `${line} entries`,
          numeric: true,
          render: (week: Week) => formatInt(week.parts_entered[line] ?? 0),
        })),
      ]}
    />
  );
}
