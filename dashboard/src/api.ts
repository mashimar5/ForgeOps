// Response types mirror the schemas in src/api.py. Every endpoint answers
// "as of" a production hour (at_hour); without one it answers as of the end
// of the data.

export interface ModelCard {
  description: string;
  evaluation: string;
  training_parts: number;
  training_cutoff_hour: number;
  scorable_parts: number;
  forward_lift_mean: number;
  forward_lift_range: number[];
  forward_top_1pct_recall_mean_pct: number;
  forward_top_1pct_recall_range_pct: number[];
}

export interface Summary {
  at_hour: number;
  data_first_hour: number;
  data_last_hour: number;
  parts_entered: number;
  parts_in_production: number;
  parts_finished: number;
  qc_results_known: number;
  qc_failure_rate_pct: number | null;
  model: ModelCard;
  note: string;
}

export interface LineStatus {
  at_hour: number;
  qc_results_last_72h: number;
  qc_failure_rate_last_72h_pct: number | null;
  qc_failure_rate_history_pct: number | null;
  ratio_to_history: number | null;
  alert: boolean;
  parts_entered_last_7_days: Record<string, number>;
  l1_share_last_7_days_pct: number | null;
  campaign: string;
  parts_in_production: number;
  note: string;
}

export interface Week {
  week: number;
  start_hour: number;
  hours_covered: number;
  qc_results: number;
  qc_failures: number;
  qc_failure_rate_pct: number | null;
  parts_entered: Record<string, number>;
}

export interface LineHistory {
  at_hour: number;
  weeks: Week[];
  note: string;
}

export interface RouteStep {
  station: string;
  hour: number;
  hours_after_entry: number;
}

export interface BatchMates {
  flagged: boolean;
  batch_size: number;
  batch_mates_failed_known: number;
  batch_mates_passed_known: number;
  first_failure_known_hour: number | null;
}

export interface RiskSummary {
  available: boolean;
  reason?: string | null;
  risk_score?: number | null;
  risk_percentile?: number | null;
  top_1_percent?: boolean | null;
}

export interface Part {
  part_id: number;
  at_hour: number;
  status: "in production" | "finished";
  entry_line: string;
  entered_hour: number;
  finished_hour: number | null;
  hours_in_production: number;
  route_so_far: RouteStep[];
  qc_result: "passed" | "failed" | null;
  batch_mates: BatchMates | null;
  twin_part_ids: number[] | null;
  risk: RiskSummary;
}

export interface Contribution {
  feature: string;
  station: string;
  value: number | null;
  contribution: number;
}

export interface Explanation {
  base_log_odds: number;
  log_odds: number;
  top_contributions: Contribution[];
  note: string;
}

export interface PartRisk extends RiskSummary {
  part_id: number;
  at_hour: number;
  explanation: Explanation | null;
  note: string;
}

export interface QueueItem {
  part_id: number;
  entry_line: string;
  finished_hour: number;
  risk_score: number;
  risk_percentile: number | null;
  top_1_percent: boolean | null;
}

export interface InspectionQueue {
  at_hour: number;
  window_hours: number;
  parts_finished_in_window: number;
  parts_scored_in_window: number;
  items: QueueItem[];
  note: string;
}

export interface BatchAlert {
  part_id: number;
  entry_line: string;
  entered_hour: number;
  hours_in_production: number;
  first_failure_known_hour: number;
  hours_since_flag: number;
  batch_size: number;
  stations_visited_so_far: number;
  last_station_so_far: string | null;
}

export interface BatchAlerts {
  at_hour: number;
  parts_in_production: number;
  flagged_parts: number;
  items: BatchAlert[];
  note: string;
}

export interface StationMetrics {
  station: string;
  line: string;
  station_number: number;
  numeric_features: number;
  parts_visited: number;
  qc_results_known: number;
  failure_rate_pct: number | null;
  risk_lift: number | null;
  median_hours_after_entry: number | null;
  median_hours_until_last_station: number | null;
}

export interface Stations {
  at_hour: number;
  stations: StationMetrics[];
}

export interface MapStation {
  station: string;
  line: string;
  parts: number;
}

/** Where the parts in production are at one hour (real line or a twin run) */
export interface LineMap {
  source: string;
  at_hour: number;
  in_production: number;
  stations: MapStation[];
  waiting_for_line3: Record<"L0" | "L1" | "other" | "total", number>;
  line3_serving: string;
  line3_started_last_hour: Record<string, number>;
  entered_last_hour: Record<string, number>;
  finished_last_hour: number;
  qc_reported_last_24h: number;
  qc_failed_last_24h: number;
  note: string;
}

export interface TwinScenario {
  id: string;
  label: string;
  description: string;
  first_hour: number;
  last_hour: number;
}

export interface TwinScenarios {
  scenarios: TwinScenario[];
  note: string;
}

export type Params = Record<string, string | number | null | undefined>;

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** GET a JSON endpoint; null and undefined parameters are left out. */
export async function getJson<T>(path: string, params: Params = {}, signal?: AbortSignal): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) query.set(key, String(value));
  }

  const search = query.toString();
  const response = await fetch(search ? `${path}?${search}` : path, { signal });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Not JSON (e.g. the API isn't running): keep the status text
    }
    throw new ApiError(response.status, detail);
  }

  return (await response.json()) as T;
}
