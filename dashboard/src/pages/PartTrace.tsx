import { useEffect, useState, type ReactNode } from "react";

import { ApiError, type BatchMates, type InspectionQueue, type Part, type PartRisk, type RouteStep } from "../api";
import { Badge } from "../components/Badge";
import { Card } from "../components/Card";
import { Legend, LineLegend, LineTag } from "../components/Legend";
import { RouteChart } from "../components/RouteChart";
import { ShapChart } from "../components/ShapChart";
import { ErrorText } from "../components/Status";
import { Table } from "../components/Table";
import { formatHour, formatScore, lineOf, weekAndDay } from "../format";
import { useApi, type Loaded } from "../hooks";
import { LINES, useColors } from "../theme";

interface PartTraceProps {
  partId: number | null;
  at: number | null;
  partLink: (id: number) => string;
  onOpen: (id: number) => void;
}

export function PartTrace({ partId, at, partLink, onOpen }: PartTraceProps) {
  const params = { at_hour: at };
  const part = useApi<Part>(partId === null ? null : `/parts/${partId}`, params);
  const risk = useApi<PartRisk>(partId === null ? null : `/parts/${partId}/risk`, { ...params, top: 10 });

  return (
    <div className="page">
      <PartSearch partId={partId} onOpen={onOpen} />

      {partId === null ? (
        <Suggestions at={at} partLink={partLink} />
      ) : part.error ? (
        <NotKnown error={part.error} />
      ) : part.data ? (
        <PartView part={part.data} risk={risk} loading={part.loading} />
      ) : (
        <p className="muted">Loading part {partId}…</p>
      )}
    </div>
  );
}

function PartSearch({ partId, onOpen }: { partId: number | null; onOpen: (id: number) => void }) {
  const [text, setText] = useState(partId === null ? "" : String(partId));

  useEffect(() => setText(partId === null ? "" : String(partId)), [partId]);

  return (
    <form
      className="search"
      role="search"
      onSubmit={(event) => {
        event.preventDefault();
        const value = text.trim();
        if (/^\d+$/.test(value)) onOpen(Number(value));
      }}
    >
      <label htmlFor="part-id">Part Id</label>
      <input
        id="part-id"
        inputMode="numeric"
        value={text}
        placeholder="e.g. 272133"
        onChange={(event) => setText(event.target.value)}
      />
      <button type="submit" className="primary">
        Trace
      </button>
    </form>
  );
}

function Suggestions({ at, partLink }: { at: number | null; partLink: (id: number) => string }) {
  const queue = useApi<InspectionQueue>("/inspection-queue", { at_hour: at, hours: 168, limit: 8 });
  const items = queue.data?.items ?? [];

  return (
    <Card
      title="Start with a part"
      subtitle="Type a part Id above, or pick one of the riskiest parts that finished in the past week"
      loading={queue.loading}
    >
      {queue.error ? (
        <ErrorText error={queue.error} />
      ) : (
        <Table
          caption="Riskiest parts of the past week"
          rows={items}
          rowKey={(item) => item.part_id}
          empty="The model has no risk scores at this hour yet; type a part Id above."
          columns={[
            { key: "part", label: "Part", render: (item) => <a href={partLink(item.part_id)}>{item.part_id}</a> },
            { key: "line", label: "Entry line", render: (item) => <LineTag line={item.entry_line} /> },
            { key: "finished", label: "Finished (hour)", numeric: true, render: (item) => formatHour(item.finished_hour) },
            { key: "score", label: "Risk score", numeric: true, render: (item) => formatScore(item.risk_score) },
          ]}
        />
      )}
    </Card>
  );
}

function NotKnown({ error }: { error: Error }) {
  if (!(error instanceof ApiError) || error.status !== 404) return <ErrorText error={error} />;

  return (
    <div className="notice">
      <p>
        <strong>{error.message}</strong>
      </p>
      <p className="muted">
        Either it isn't in the data, it hasn't entered production by this hour, or it is a repeat test record, which only
        appears once its part's QC result is reported.
      </p>
    </div>
  );
}

function PartView({ part, risk, loading }: { part: Part; risk: Loaded<PartRisk>; loading: boolean }) {
  const lines = LINES.filter((line) => part.route_so_far.some((step) => lineOf(step.station) === line));
  const entered = weekAndDay(part.entered_hour);

  return (
    <div className={loading ? "is-loading" : undefined}>
      <section className="part-head">
        <h1>Part {part.part_id}</h1>
        <div className="badges">
          <Badge tone="neutral">{part.status === "finished" ? "Finished" : "In production"}</Badge>
          <QcBadge part={part} />
          {part.batch_mates?.flagged && <Badge tone="warning">Flagged by the batch-mate alert</Badge>}
        </div>
      </section>

      <dl className="facts">
        <Fact label="Entry line" value={<LineTag line={part.entry_line} />} />
        <Fact label="Entered" value={`Hour ${formatHour(part.entered_hour)}`} detail={`Week ${entered.week}, day ${entered.day}`} />
        <Fact label="Finished" value={part.finished_hour === null ? "Not yet" : `Hour ${formatHour(part.finished_hour)}`} />
        <Fact label="Hours in production" value={formatHour(part.hours_in_production)} />
        <Fact label="Stations visited" value={String(part.route_so_far.length)} />
        <Fact
          label="Repeat tests"
          value={part.twin_part_ids === null ? "Shown after QC" : part.twin_part_ids.length ? part.twin_part_ids.join(", ") : "None"}
          detail={part.twin_part_ids?.length ? "Twin records: same measurements and timestamps" : undefined}
        />
      </dl>

      <div className="grid-2">
        <Card
          title="Route so far"
          subtitle="Each station the part has visited, by hours after it entered"
          table={<RouteTable route={part.route_so_far} />}
        >
          <LineLegend lines={lines} mark="dot" />
          <RouteChart route={part.route_so_far} />
        </Card>

        <RiskCard part={part} risk={risk} />
      </div>

      {part.batch_mates && <BatchMatesCard mates={part.batch_mates} />}
    </div>
  );
}

function QcBadge({ part }: { part: Part }) {
  if (part.qc_result === "failed") return <Badge tone="critical">Failed final QC</Badge>;
  if (part.qc_result === "passed") return <Badge tone="good">Passed final QC</Badge>;
  return <Badge tone="neutral">QC result not reported yet</Badge>;
}

function Fact({ label, value, detail }: { label: string; value: ReactNode; detail?: string }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd>
        {value}
        {detail && <span className="fact-detail">{detail}</span>}
      </dd>
    </div>
  );
}

function RouteTable({ route }: { route: RouteStep[] }) {
  return (
    <Table
      caption="Stations visited"
      rows={route}
      rowKey={(step) => step.station}
      columns={[
        { key: "station", label: "Station", render: (step) => <LineTag line={lineOf(step.station)}>{step.station}</LineTag> },
        { key: "hour", label: "Production hour", numeric: true, render: (step) => formatHour(step.hour) },
        { key: "after", label: "Hours after entry", numeric: true, render: (step) => formatHour(step.hours_after_entry) },
      ]}
    />
  );
}

function RiskCard({ part, risk }: { part: Part; risk: Loaded<PartRisk> }) {
  const colors = useColors();
  const summary = part.risk;

  if (!summary.available) {
    return (
      <Card title="Risk score" subtitle="Ranks finished parts for final-QC inspection">
        <p className="empty">{summary.reason}</p>
      </Card>
    );
  }

  const explanation = risk.data?.explanation ?? null;

  return (
    <Card
      title="Risk score"
      subtitle="Ranks finished parts for final-QC inspection; not a probability"
      loading={risk.loading}
      note={explanation?.note}
      table={
        explanation && (
          <Table
            caption="Measurements that move the risk score most"
            rows={explanation.top_contributions}
            rowKey={(row) => row.feature}
            columns={[
              { key: "feature", label: "Measurement", render: (row) => row.feature },
              { key: "value", label: "Value", numeric: true, render: (row) => (row.value === null ? "missing" : String(row.value)) },
              {
                key: "contribution",
                label: "Log-odds",
                numeric: true,
                render: (row) => `${row.contribution > 0 ? "+" : ""}${row.contribution.toFixed(3)}`,
              },
            ]}
          />
        )
      }
    >
      <div className="risk-line">
        <span className="risk-score">{formatScore(summary.risk_score)}</span>
        <span className="muted">
          {summary.risk_percentile === null || summary.risk_percentile === undefined
            ? "Too few parts scored yet to rank it"
            : `Percentile ${summary.risk_percentile.toFixed(2)} among parts scored so far`}
        </span>
        {summary.top_1_percent && <Badge tone="warning">Top 1%</Badge>}
      </div>

      {explanation ? (
        <>
          <Legend
            mark="rect"
            items={[
              { label: "Pushes toward failing", color: colors.towardFail },
              { label: "Pushes toward passing", color: colors.towardPass },
            ]}
          />
          <ShapChart contributions={explanation.top_contributions} />
          <p className="small muted">
            The model's average log-odds is {explanation.base_log_odds.toFixed(2)}; this part's is {explanation.log_odds.toFixed(2)}.
            The {explanation.top_contributions.length} largest of 968 contributions are shown.
          </p>
        </>
      ) : (
        risk.error && <ErrorText error={risk.error} />
      )}
    </Card>
  );
}

function BatchMatesCard({ mates }: { mates: BatchMates }) {
  const unknown = mates.batch_size - 1 - mates.batch_mates_failed_known - mates.batch_mates_passed_known;

  return (
    <Card title="Batch-mates" subtitle={`The other parts that entered in the same 6-minute tick (batch of ${mates.batch_size})`}>
      <dl className="facts">
        <Fact label="Failed final QC" value={String(mates.batch_mates_failed_known)} />
        <Fact label="Passed final QC" value={String(mates.batch_mates_passed_known)} />
        <Fact label="QC result not reported yet" value={String(unknown)} />
        <Fact
          label="Flagged"
          value={mates.first_failure_known_hour === null ? "No" : `Since hour ${formatHour(mates.first_failure_known_hour)}`}
        />
      </dl>
    </Card>
  );
}
