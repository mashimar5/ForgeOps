import { Bar, BarChart, CartesianGrid, Cell, LabelList, ReferenceLine, Tooltip, XAxis, YAxis } from "recharts";

import type { Contribution } from "../api";
import { axisTick, useColors, type Colors } from "../theme";
import { TooltipBox, tipPoint, type TipProps } from "./TooltipBox";

interface ShapPoint extends Contribution {
  label: string;
}

const ROW = 30;

function signed(value: number): string {
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}`;
}

/** SHAP contributions in log-odds: red pushes toward failure, blue toward passing */
export function ShapChart({ contributions }: { contributions: Contribution[] }) {
  const colors = useColors();
  const data: ShapPoint[] = contributions.map((c) => ({
    ...c,
    label: `${c.feature} ${c.value === null ? "(missing)" : `= ${c.value}`}`,
  }));

  return (
    <BarChart
      data={data}
      layout="vertical"
      responsive
      width="100%"
      height={data.length * ROW + 36}
      margin={{ top: 4, right: 52, bottom: 4, left: 52 }}
    >
      <CartesianGrid horizontal={false} stroke={colors.grid} />
      <XAxis
        type="number"
        tick={axisTick(colors)}
        tickLine={false}
        axisLine={{ stroke: colors.axis }}
        tickFormatter={signed}
      />
      <YAxis
        type="category"
        dataKey="label"
        width={200}
        tick={{ fill: colors.ink2, fontSize: 12 }}
        tickLine={false}
        axisLine={false}
      />
      <ReferenceLine x={0} stroke={colors.axis} strokeWidth={1} />
      <Tooltip
        isAnimationActive={false}
        cursor={{ fill: colors.grid, fillOpacity: 0.5 }}
        content={(props) => <ShapTip active={props.active} payload={props.payload} />}
      />
      <Bar
        dataKey="contribution"
        maxBarSize={18}
        isAnimationActive={false}
        shape={(props: { x?: number; y?: number; width?: number; height?: number; payload?: ShapPoint }) => (
          <DataEndBar {...props} colors={colors} />
        )}
      >
        {data.map((point) => (
          <Cell key={point.feature} fill={point.contribution >= 0 ? colors.towardFail : colors.towardPass} />
        ))}
        <LabelList dataKey="contribution" content={(props) => <EndLabel {...props} color={colors.ink2} />} />
      </Bar>
    </BarChart>
  );
}

/** A horizontal bar with its rounded end at the value, on either side of zero */
function DataEndBar({
  x = 0,
  y = 0,
  width = 0,
  height = 0,
  payload,
  colors,
}: {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  payload?: ShapPoint;
  colors: Colors;
}) {
  if (!payload) return null;

  const left = Math.min(x, x + width);
  const w = Math.abs(width);
  const r = Math.min(4, w / 2, height / 2);
  const fill = payload.contribution >= 0 ? colors.towardFail : colors.towardPass;

  const path =
    payload.contribution >= 0
      ? `M${left},${y} h${w - r} a${r},${r} 0 0 1 ${r},${r} v${height - 2 * r} a${r},${r} 0 0 1 ${-r},${r} h${-(w - r)} Z`
      : `M${left + w},${y} h${-(w - r)} a${r},${r} 0 0 0 ${-r},${r} v${height - 2 * r} a${r},${r} 0 0 0 ${r},${r} h${w - r} Z`;

  return <path d={path} fill={fill} />;
}

/** The value at the bar's end, outside the bar */
function EndLabel({ viewBox, value, color }: { viewBox?: unknown; value?: unknown; color: string }) {
  const box = viewBox as { x?: number; y?: number; width?: number; height?: number } | undefined;
  const number = Number(value);
  if (!box || box.x === undefined || box.y === undefined || !Number.isFinite(number)) return null;

  const width = box.width ?? 0;
  const left = Math.min(box.x, box.x + width);
  const right = Math.max(box.x, box.x + width);
  const positive = number >= 0;

  return (
    <text
      x={positive ? right + 6 : left - 6}
      y={box.y + (box.height ?? 0) / 2}
      textAnchor={positive ? "start" : "end"}
      dominantBaseline="central"
      fill={color}
      fontSize={12}
      style={{ fontVariantNumeric: "tabular-nums" }}
    >
      {signed(number)}
    </text>
  );
}

function ShapTip(props: TipProps) {
  const colors = useColors();
  const point = tipPoint<ShapPoint>(props);
  if (!point) return null;

  return (
    <TooltipBox
      title={point.feature}
      rows={[
        {
          label: "log-odds",
          value: signed(point.contribution),
          color: point.contribution >= 0 ? colors.towardFail : colors.towardPass,
        },
        { label: "measured value", value: point.value === null ? "missing" : String(point.value) },
        { label: "station", value: point.station },
      ]}
    />
  );
}
