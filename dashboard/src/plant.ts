import { createContext, useContext } from "react";

import type { Plant } from "./api";

// Illustrative names over the anonymized codes (src/plant_names.py). The
// codes stay visible next to every name; without the names (API older than
// /plant, or still loading) everything falls back to the codes.

export const PlantContext = createContext<Plant | undefined>(undefined);

export interface Names {
  ready: boolean;
  lineName: (code: string) => string;
  station: (code: string) => { op: string; label: string; cell: string | null };
}

export function useNames(): Names {
  const plant = useContext(PlantContext);
  const lines = new Map(plant?.lines.map((l) => [l.code, l.name]));
  const stations = new Map(plant?.stations.map((s) => [s.station, s]));

  return {
    ready: plant !== undefined,
    lineName: (code) => lines.get(code) ?? code,
    station: (code) => {
      const s = stations.get(code);
      return s ? { op: s.op, label: s.label, cell: s.cell } : { op: code.split("_")[1] ?? code, label: code, cell: null };
    },
  };
}
