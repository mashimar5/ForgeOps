import { CartesianGrid, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from "recharts";

import type { RouteStep } from "../api";
import { formatHour, lineOf, stationNumber } from "../format";
import { useNames } from "../plant";
import { axisTick, lineColor, useColors } from "../theme";
import { TooltipBox, tipPoint, type TipProps } from "./TooltipBox";

interface RoutePoint extends RouteStep {
  number: number;
  line: string;
}

// Where each line starts, in production order (station numbers)
const LINE_STARTS: Record<number, string> = { 0: "L0 · S0", 24: "L1 · S24", 26: "L2 · S26", 29: "L3 · S29", 51: "S51" };
const LINE_START_CODE: Record<number, string> = { 0: "L0", 24: "L1", 26: "L2", 29: "L3" };

/** A part's journey: each station it visited, by hours after it entered */
export function RouteChart({ route }: { route: RouteStep[] }) {
  const colors = useColors();
  const names = useNames();
  const points: RoutePoint[] = route.map((step) => ({ ...step, number: stationNumber(step.station), line: lineOf(step.station) }));

  return (
    <ScatterChart responsive width="100%" height={280} margin={{ top: 12, right: 16, bottom: 4, left: 0 }}>
      <CartesianGrid stroke={colors.grid} vertical={false} />
      <XAxis
        type="number"
        dataKey="hours_after_entry"
        name="Hours after entry"
        domain={[0, "dataMax"]}
        tick={axisTick(colors)}
        tickLine={false}
        axisLine={{ stroke: colors.axis }}
        tickFormatter={(hours: number) => `${formatHour(hours)} h`}
      />
      <YAxis
        type="number"
        dataKey="number"
        domain={[0, 51]}
        ticks={Object.keys(LINE_STARTS).map(Number)}
        tick={axisTick(colors)}
        tickLine={false}
        axisLine={false}
        width={names.ready ? 128 : 72}
        tickFormatter={(number: number) =>
          names.ready && number in LINE_START_CODE ? names.lineName(LINE_START_CODE[number]) : LINE_STARTS[number] ?? `S${number}`
        }
      />
      <Tooltip
        isAnimationActive={false}
        cursor={{ stroke: colors.axis, strokeWidth: 1 }}
        content={(props) => <StepTip active={props.active} payload={props.payload} />}
      />
      <Scatter
        data={points}
        line={{ stroke: colors.axis, strokeWidth: 1 }}
        isAnimationActive={false}
        shape={(props: { cx?: number; cy?: number; payload?: RoutePoint }) =>
          props.cx === undefined || props.cy === undefined || !props.payload ? (
            <g />
          ) : (
            <circle
              cx={props.cx}
              cy={props.cy}
              r={4}
              fill={lineColor(colors, props.payload.line)}
              stroke={colors.surface}
              strokeWidth={2}
            />
          )
        }
      />
    </ScatterChart>
  );
}

function StepTip(props: TipProps) {
  const colors = useColors();
  const names = useNames();
  const step = tipPoint<RoutePoint>(props);
  if (!step) return null;

  return (
    <TooltipBox
      title={names.ready ? `${names.station(step.station).label} (${step.station})` : step.station}
      rows={[
        { label: "hours after entry", value: formatHour(step.hours_after_entry), color: lineColor(colors, step.line) },
        { label: "production hour", value: formatHour(step.hour) },
      ]}
    />
  );
}
