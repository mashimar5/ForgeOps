import { createContext, useContext } from "react";

export const LINES = ["L0", "L1", "L2", "L3"] as const;
export type Line = (typeof LINES)[number];

export interface Colors {
  surface: string;
  ink: string;
  ink2: string;
  muted: string;
  grid: string;
  axis: string;
  /** One color per production line, the same on every chart */
  lines: Record<Line, string>;
  /** Measures over all lines together, so no line's color */
  total: string;
  /** SHAP contributions: toward failure / toward passing */
  towardFail: string;
  towardPass: string;
  /** Line map: one blue ramp for part counts (COUNT_BINS), text that reads on each step, and "none" */
  ramp: string[];
  rampText: string[];
  empty: string;
}

/** Part-count bands for the line map: [1, 10), [10, 50), ... [5000, inf) */
export const COUNT_BINS = [1, 10, 50, 200, 1000, 5000];

export function countStep(count: number): number {
  let step = -1;
  for (const [i, lower] of COUNT_BINS.entries()) if (count >= lower) step = i;
  return step;
}

// The dataviz skill's reference palette. Lines take categorical slots 1-4
// (blue, orange, aqua, yellow), validated as an adjacent set in both modes;
// aqua and yellow are below 3:1 on the light surface, so every chart has a
// legend and a table view. Totals take violet (slot 7); SHAP uses the
// blue-red diverging pair.
export const PALETTE: Record<"light" | "dark", Colors> = {
  light: {
    surface: "#fcfcfb",
    ink: "#0b0b0b",
    ink2: "#52514e",
    muted: "#898781",
    grid: "#e1e0d9",
    axis: "#c3c2b7",
    lines: { L0: "#2a78d6", L1: "#eb6834", L2: "#1baf7a", L3: "#eda100" },
    total: "#4a3aa7",
    towardFail: "#e34948",
    towardPass: "#2a78d6",
    // Sequential blue steps 100-600, light to dark
    ramp: ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"],
    rampText: ["#0b0b0b", "#0b0b0b", "#0b0b0b", "#ffffff", "#ffffff", "#ffffff"],
    empty: "#f2f1ed",
  },
  dark: {
    surface: "#1a1a19",
    ink: "#ffffff",
    ink2: "#c3c2b7",
    muted: "#898781",
    grid: "#2c2c2a",
    axis: "#383835",
    lines: { L0: "#3987e5", L1: "#d95926", L2: "#199e70", L3: "#c98500" },
    total: "#9085e9",
    towardFail: "#e66767",
    towardPass: "#3987e5",
    // The same steps reversed: on the dark surface more parts read lighter
    ramp: ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"],
    rampText: ["#ffffff", "#ffffff", "#ffffff", "#0b0b0b", "#0b0b0b", "#0b0b0b"],
    empty: "#232321",
  },
};

export const ColorsContext = createContext<Colors>(PALETTE.light);

export function useColors(): Colors {
  return useContext(ColorsContext);
}

export function lineColor(colors: Colors, line: string): string {
  return colors.lines[line as Line] ?? colors.muted;
}

/** Axis tick text: muted ink, small */
export function axisTick(colors: Colors) {
  return { fill: colors.muted, fontSize: 12 };
}
