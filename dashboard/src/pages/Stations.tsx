import type { StationMetrics, Stations, Summary } from "../api";
import { Card } from "../components/Card";
import { LineLegend, LineTag } from "../components/Legend";
import { StationChart } from "../components/StationChart";
import { Placeholder } from "../components/Status";
import { Table } from "../components/Table";
import { formatHour, formatInt, formatPct } from "../format";
import { useApi } from "../hooks";
import { LINES, useColors } from "../theme";

function hours(value: number | null): string {
  return value === null ? "–" : formatHour(value);
}

export function StationsPage({ at }: { at: number | null }) {
  const colors = useColors();
  const params = { at_hour: at };
  const stations = useApi<Stations>("/stations", params);
  const summary = useApi<Summary>("/summary", params);

  const rows = stations.data?.stations;

  return (
    <div className="page">
      <Card
        title="QC failure rate by station"
        subtitle="QC results of the parts that visited each station, in production order"
        loading={stations.loading}
        note="Station failure rates are associations, not causes: parts may be routed to a station because they are already suspect. Rates count every QC record, repeat tests included."
      >
        <LineLegend
          lines={LINES}
          mark="rect"
          extra={
            summary.data?.qc_failure_rate_pct != null
              ? [{ label: `All parts ${formatPct(summary.data.qc_failure_rate_pct, 2)}`, color: colors.ink2, mark: "line" }]
              : []
          }
        />
        {rows ? (
          <StationChart stations={rows} overallPct={summary.data?.qc_failure_rate_pct ?? null} />
        ) : (
          <Placeholder error={stations.error} height={300} />
        )}
      </Card>

      <Card
        title="All stations"
        subtitle="The chart's numbers and more; select a column to sort"
        loading={stations.loading}
        note="Risk lift is a station's failure rate divided by the rate for all parts. Median hours are counted from a part's entry, and from the station to the part's last station."
      >
        {rows && (
          <Table<StationMetrics>
            caption="Metrics for every station"
            rows={rows}
            rowKey={(row) => row.station}
            initialSort={{ key: "station", descending: false }}
            columns={[
              {
                key: "station",
                label: "Station",
                render: (row) => <LineTag line={row.line}>{row.station}</LineTag>,
                sortValue: (row) => row.station_number,
              },
              {
                key: "parts",
                label: "Parts visited",
                numeric: true,
                render: (row) => formatInt(row.parts_visited),
                sortValue: (row) => row.parts_visited,
              },
              {
                key: "results",
                label: "QC results",
                numeric: true,
                render: (row) => formatInt(row.qc_results_known),
                sortValue: (row) => row.qc_results_known,
              },
              {
                key: "rate",
                label: "Failure rate",
                numeric: true,
                render: (row) => formatPct(row.failure_rate_pct, 3),
                sortValue: (row) => row.failure_rate_pct,
              },
              {
                key: "lift",
                label: "Risk lift",
                numeric: true,
                render: (row) => (row.risk_lift === null ? "–" : `${row.risk_lift.toFixed(2)}×`),
                sortValue: (row) => row.risk_lift,
              },
              {
                key: "after",
                label: "Median h after entry",
                numeric: true,
                render: (row) => hours(row.median_hours_after_entry),
                sortValue: (row) => row.median_hours_after_entry,
              },
              {
                key: "until",
                label: "Median h to last station",
                numeric: true,
                render: (row) => hours(row.median_hours_until_last_station),
                sortValue: (row) => row.median_hours_until_last_station,
              },
              {
                key: "features",
                label: "Measurements",
                numeric: true,
                render: (row) => formatInt(row.numeric_features),
                sortValue: (row) => row.numeric_features,
              },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
