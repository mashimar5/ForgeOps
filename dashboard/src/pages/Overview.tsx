import type { BatchAlerts, InspectionQueue, LineHistory, LineStatus, Summary } from "../api";
import { Badge } from "../components/Badge";
import { Card } from "../components/Card";
import { Legend, LineLegend, LineTag } from "../components/Legend";
import { StatTile } from "../components/StatTile";
import { ErrorText, Placeholder } from "../components/Status";
import { Table } from "../components/Table";
import { MIN_WEEKLY_RESULTS, WeeklyEntriesChart, WeeklyRateChart, WeeklyTable } from "../components/WeeklyCharts";
import { formatHour, formatInt, formatPct, formatScore } from "../format";
import { useApi } from "../hooks";
import { LINES, useColors } from "../theme";

interface OverviewProps {
  at: number | null;
  partLink: (id: number) => string;
}

export function Overview({ at, partLink }: OverviewProps) {
  const colors = useColors();
  const params = { at_hour: at };
  const summary = useApi<Summary>("/summary", params);
  const line = useApi<LineStatus>("/line/status", params);
  const history = useApi<LineHistory>("/line/history", params);
  const queue = useApi<InspectionQueue>("/inspection-queue", { ...params, hours: 24, limit: 20 });
  const alerts = useApi<BatchAlerts>("/alerts/batch-mates", { ...params, limit: 20 });

  const s = summary.data;
  const l = line.data;
  const a = alerts.data;
  const q = queue.data;
  const weeks = history.data?.weeks;

  return (
    <div className="page">
      <section className="tiles" aria-label="The line at this hour">
        <StatTile
          hero
          label="QC failure rate, last 72 hours"
          value={l ? formatPct(l.qc_failure_rate_last_72h_pct, 3) : "…"}
          detail={l && <LineMonitor status={l} />}
        />
        <StatTile
          label="Parts in production"
          value={s ? formatInt(s.parts_in_production) : "…"}
          detail={s && `${formatInt(s.parts_entered)} entered so far`}
        />
        <StatTile
          label="Parts finished"
          value={s ? formatInt(s.parts_finished) : "…"}
          detail={s && `${formatInt(s.qc_results_known)} QC results reported`}
        />
        <StatTile
          label="QC failure rate, all results"
          value={s ? formatPct(s.qc_failure_rate_pct, 3) : "…"}
          detail="Every QC record so far, repeat tests included"
        />
        <StatTile
          label="Flagged by the batch-mate alert"
          value={a ? formatInt(a.flagged_parts) : "…"}
          detail={a && `of ${formatInt(a.parts_in_production)} parts in production`}
        />
      </section>

      <div className="grid-2">
        <Card
          title="QC failure rate by week"
          subtitle="Every QC result, in the week it was reported"
          loading={history.loading}
          table={weeks && <WeeklyTable weeks={weeks} />}
          note={`Weeks with fewer than ${MIN_WEEKLY_RESULTS} results are left out: the production break in weeks 50–51, and the first hours of a week.`}
        >
          <Legend
            items={[
              { label: "Weekly rate", color: colors.total, mark: "line" },
              ...(s && s.qc_failure_rate_pct !== null
                ? [{ label: `All results so far ${formatPct(s.qc_failure_rate_pct, 2)}`, color: colors.ink2, mark: "line" as const }]
                : []),
            ]}
          />
          {weeks ? (
            <WeeklyRateChart weeks={weeks} overallPct={s?.qc_failure_rate_pct ?? null} />
          ) : (
            <Placeholder error={history.error} />
          )}
        </Card>

        <Card
          title="Parts entering production by week"
          subtitle={l ? `Each part once, by entry line. Last 7 days: ${campaignLabel(l)}, ${entriesText(l)}` : "Each part once, by entry line"}
          loading={history.loading}
          table={weeks && <WeeklyTable weeks={weeks} />}
          note="The factory alternates between entry lines L0 and L1 in campaigns; L1 parts wait about 13 days before line 3. Week 0's L3 entries are parts already on line 3 when the data starts."
        >
          <LineLegend lines={LINES} mark="rect" />
          {weeks ? <WeeklyEntriesChart weeks={weeks} /> : <Placeholder error={history.error} />}
        </Card>
      </div>

      <div className="grid-2 align-start">
        <Card
          title="Inspection queue"
          subtitle={
            q
              ? `Riskiest of the ${formatInt(q.parts_scored_in_window)} parts scored in the last 24 hours`
              : "Riskiest parts that finished in the last 24 hours"
          }
          loading={queue.loading}
          note={
            s &&
            `Risk scores rank finished parts for final-QC inspection; they are not probabilities. In forward tests, ` +
              `inspecting the top 1% caught about ${Math.round(s.model.forward_top_1pct_recall_mean_pct)}% of failures ` +
              `(${s.model.forward_top_1pct_recall_range_pct.map((v) => Math.round(v)).join("–")}% across 4 test periods).`
          }
        >
          {queue.error ? (
            <ErrorText error={queue.error} />
          ) : (
            q && (
              <Table
                caption="Inspection queue"
                rows={q.items}
                rowKey={(item) => item.part_id}
                empty={
                  q.parts_finished_in_window === 0
                    ? "No parts finished in the last 24 hours."
                    : s && `No risk scores yet: the model trained on the parts that finished by hour ${formatHour(s.model.training_cutoff_hour)}.`
                }
                columns={[
                  { key: "part", label: "Part", render: (item) => <a href={partLink(item.part_id)}>{item.part_id}</a> },
                  { key: "line", label: "Entry line", render: (item) => <LineTag line={item.entry_line} /> },
                  { key: "finished", label: "Finished (hour)", numeric: true, render: (item) => formatHour(item.finished_hour) },
                  { key: "score", label: "Risk score", numeric: true, render: (item) => formatScore(item.risk_score) },
                  {
                    key: "percentile",
                    label: "Percentile",
                    numeric: true,
                    render: (item) => (item.risk_percentile === null ? "–" : item.risk_percentile.toFixed(2)),
                  },
                ]}
              />
            )
          )}
        </Card>

        <Card
          title="Batch-mate alerts"
          subtitle={
            a
              ? `${formatInt(a.flagged_parts)} flagged parts in production` +
                (a.flagged_parts > a.items.length ? `; the ${a.items.length} most recently flagged` : "")
              : "Parts in production whose batch-mate already failed final QC"
          }
          loading={alerts.loading}
          note={a?.note}
        >
          {alerts.error ? (
            <ErrorText error={alerts.error} />
          ) : (
            a && (
              <Table
                caption="Batch-mate alerts"
                rows={a.items}
                rowKey={(item) => item.part_id}
                empty="No parts are flagged at this hour."
                columns={[
                  { key: "part", label: "Part", render: (item) => <a href={partLink(item.part_id)}>{item.part_id}</a> },
                  { key: "line", label: "Entry line", render: (item) => <LineTag line={item.entry_line} /> },
                  { key: "flag", label: "Flagged (h ago)", numeric: true, render: (item) => formatHour(item.hours_since_flag) },
                  {
                    key: "waiting",
                    label: "In production (h)",
                    numeric: true,
                    render: (item) => formatHour(item.hours_in_production),
                  },
                  { key: "batch", label: "Batch size", numeric: true, render: (item) => formatInt(item.batch_size) },
                  { key: "last", label: "Last station", render: (item) => item.last_station_so_far ?? "–" },
                ]}
              />
            )
          )}
        </Card>
      </div>
    </div>
  );
}

function LineMonitor({ status }: { status: LineStatus }) {
  const compared =
    status.ratio_to_history === null
      ? "Too few QC results in the last 72 hours to compare"
      : `${status.ratio_to_history.toFixed(2)}× the rate so far (${formatPct(status.qc_failure_rate_history_pct, 3)}), ` +
        `${formatInt(status.qc_results_last_72h)} results`;

  return (
    <>
      <span>{compared}</span>
      <span className="tile-badges">
        {status.alert ? <Badge tone="critical">Line monitor alert</Badge> : <Badge tone="good">No line monitor alert</Badge>}
      </span>
      <span className="tile-note">{status.note}</span>
    </>
  );
}

function campaignLabel(status: LineStatus): string {
  if (status.campaign === "L1 campaign") return "an L1 campaign";
  if (status.campaign === "L0 only") return "L0 only";
  return "too little production to tell";
}

function entriesText(status: LineStatus): string {
  const entered = status.parts_entered_last_7_days;
  const share = status.l1_share_last_7_days_pct === null ? "" : ` (L1 ${status.l1_share_last_7_days_pct.toFixed(1)}%)`;
  return `${formatInt(entered.L0 ?? 0)} parts on L0, ${formatInt(entered.L1 ?? 0)} on L1${share}`;
}
