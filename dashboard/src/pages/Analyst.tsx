import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type Ref } from "react";

import { ask, newConversation, stop, useConversation, type Exchange, type ToolStep } from "../analyst";
import type { AnalystStatus } from "../api";
import { Badge } from "../components/Badge";
import { Card } from "../components/Card";
import { Inline, Markdown } from "../components/Markdown";
import { ErrorText } from "../components/Status";
import { formatCompact, formatHour, formatInt, weekAndDay } from "../format";
import { toHash, useApi } from "../hooks";

const SUGGESTIONS = [
  "How is the line doing right now?",
  "Which parts should we inspect first, and why?",
  "Explain the risk score of the top part in the inspection queue.",
  "Are any parts in production flagged by batch-mate alerts?",
  "Which stations have the highest QC failure rates?",
  "Is an L1 campaign running?",
];

const TOOL_LABELS: Record<string, string> = {
  get_factory_summary: "Factory summary",
  get_line_status: "Line status",
  get_part: "Part",
  explain_part_risk: "Risk explanation",
  get_inspection_queue: "Inspection queue",
  get_batch_mate_alerts: "Batch-mate alerts",
  get_station: "Station",
  list_stations: "All stations",
};

function hourLabel(hour: number | null, lastHour: number): string {
  const value = hour ?? lastHour;
  const { week, day } = weekAndDay(value);
  return `${hour === null ? "the end of the data, " : ""}hour ${formatHour(value)} (week ${week}, day ${day})`;
}

function formatBytes(chars: number): string {
  return chars < 1000 ? `${formatInt(chars)} chars` : `${formatCompact(chars)} chars`;
}

function formatCost(usd: number): string {
  return `$${usd < 0.1 ? usd.toFixed(3) : usd.toFixed(2)}`;
}

export function AnalystPage({ at, lastHour }: { at: number | null; lastHour: number }) {
  const status = useApi<AnalystStatus>("/analyst/status");
  const conversation = useConversation();
  const [draft, setDraft] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const latest = useRef<HTMLElement>(null);

  const started = conversation.exchanges.length > 0;
  const hour = started ? conversation.atHour : at;
  const moved = started && conversation.atHour !== at;
  const available = status.data?.available ?? false;
  const canAsk = available && !conversation.running && draft.trim().length > 0;

  // Bring a new question into view as it starts
  const count = conversation.exchanges.length;
  useEffect(() => {
    if (count > 0) latest.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [count]);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!canAsk) return;
    void ask(draft.trim(), hour);
    setDraft("");
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
  }

  function pick(question: string) {
    setDraft(question);
    input.current?.focus();
  }

  return (
    <div className="page analyst">
      <Card
        title="AI Analyst"
        subtitle="Ask about the line in plain language. Claude answers from the same evidence as the other pages and lists every tool call it makes, so each number can be checked."
        note={status.data?.note}
      >
        {status.error && <ErrorText error={status.error} />}
        {status.data && !status.data.available && status.data.reason && (
          <p className="error-text">
            <Inline text={status.data.reason} />
          </p>
        )}
        {status.data && (
          <p className="analyst-meta muted small">
            {status.data.model} · {status.data.tools.length} read-only tools · effort {status.data.effort} · at most{" "}
            {status.data.max_tool_rounds} rounds of tool calls per question · roughly $0.10 a question so far
          </p>
        )}
      </Card>

      {moved && (
        <div className="notice analyst-moved" role="status">
          <p>
            This conversation answers as of {hourLabel(conversation.atHour, lastHour)}, and follow-ups stay there.
            The time control is now at {hourLabel(at, lastHour)}.
          </p>
          <button type="button" onClick={newConversation}>
            Start a new conversation at hour {formatHour(at ?? lastHour)}
          </button>
        </div>
      )}

      <section className="analyst-thread" aria-label="Conversation" aria-live="polite">
        {!started ? (
          <div className="suggestions">
            <p className="muted small">Try a question (it fills the box; nothing is sent until you ask):</p>
            <ul>
              {SUGGESTIONS.map((question) => (
                <li key={question}>
                  <button type="button" onClick={() => pick(question)}>
                    {question}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          conversation.exchanges.map((exchange, i) => (
            <ExchangeView
              key={i}
              ref={i === conversation.exchanges.length - 1 ? latest : undefined}
              exchange={exchange}
              running={conversation.running && i === conversation.exchanges.length - 1}
              hour={conversation.atHour}
              lastHour={lastHour}
              model={status.data?.model}
            />
          ))
        )}
      </section>

      <form className="analyst-composer" onSubmit={submit}>
        <label className="visually-hidden" htmlFor="analyst-question">
          Question
        </label>
        <textarea
          id="analyst-question"
          ref={input}
          rows={2}
          maxLength={2000}
          value={draft}
          placeholder={started ? "Ask a follow-up…" : "Ask about the line…"}
          disabled={!available}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="analyst-actions">
          <span className="muted small">
            As of {hourLabel(hour, lastHour)}. Enter asks, Shift+Enter adds a line.
          </span>
          {started && (
            <button type="button" onClick={newConversation}>
              New conversation
            </button>
          )}
          {conversation.running ? (
            <button type="button" onClick={stop}>
              Stop
            </button>
          ) : (
            <button type="submit" className="primary" disabled={!canAsk}>
              Ask
            </button>
          )}
        </div>
      </form>
    </div>
  );
}

// ============================================================
// ONE QUESTION AND ITS ANSWER
// ============================================================

interface ExchangeProps {
  exchange: Exchange;
  running: boolean;
  hour: number | null;
  lastHour: number;
  model: string | undefined;
  ref?: Ref<HTMLElement>;
}

function ExchangeView({ exchange, running, hour, lastHour, model, ref }: ExchangeProps) {
  const { steps, done } = exchange;
  const tools = steps.filter((step): step is ToolStep => step.kind === "tool");
  const toolTurns = new Set(tools.map((step) => step.turn));
  const lastTurn = Math.max(-1, ...steps.map((step) => step.turn));

  // The answer is the text of the last turn, once no tool call follows it;
  // text in earlier turns is a note between tool calls
  const answerTurn = toolTurns.has(lastTurn) ? -1 : lastTurn;
  const streamed = steps.find((step) => step.kind === "text" && step.turn === answerTurn);
  const answer = done ? done.answer : streamed?.kind === "text" ? streamed.text : "";
  const evidence = steps.filter((step) => step.kind === "tool" || step.turn !== answerTurn);

  const servedBy = done?.models.filter((name) => name !== model) ?? [];
  const tokensIn = done
    ? done.usage.input_tokens + done.usage.cache_read_input_tokens + done.usage.cache_creation_input_tokens
    : 0;

  return (
    <article className="exchange" ref={ref}>
      <p className="exchange-question">
        <span className="visually-hidden">You asked: </span>
        {exchange.question}
      </p>

      {evidence.length > 0 && (
        <div className="evidence">
          <h3>
            Evidence · {tools.length} tool call{tools.length === 1 ? "" : "s"}
          </h3>
          <ol>
            {evidence.map((step, i) =>
              step.kind === "tool" ? (
                <ToolRow key={step.id} step={step} hour={hour} />
              ) : (
                <li key={`note-${i}`} className="evidence-note">
                  {step.text}
                </li>
              ),
            )}
          </ol>
        </div>
      )}

      {answer ? (
        <Markdown className="answer" text={answer} />
      ) : (
        running && <p className="muted working">{tools.length > 0 ? "Reading the evidence…" : "Thinking…"}</p>
      )}

      {exchange.error && <p className="error-text">{exchange.error}</p>}
      {exchange.stopped && <p className="muted small">Stopped. The analyst forgets this question.</p>}

      {done && (
        <p className="exchange-meta muted small">
          As of {hourLabel(hour, lastHour)} · {done.tool_calls} tool call{done.tool_calls === 1 ? "" : "s"} ·{" "}
          {formatCompact(tokensIn)} tokens in, {formatCompact(done.usage.output_tokens)} out
          {done.cost_usd !== null && ` · about ${formatCost(done.cost_usd)}`} · {done.seconds.toFixed(0)} s
          {servedBy.length > 0 && ` · answered by ${servedBy.join(", ")}`}
          {!done.kept && " · not kept for follow-ups"}
        </p>
      )}
    </article>
  );
}

function ToolRow({ step, hour }: { step: ToolStep; hour: number | null }) {
  const [open, setOpen] = useState(false);
  const call = `${step.name}(${Object.entries(step.input)
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(", ")})`;

  // A part the analyst looked up opens in Part trace, at the hour it asked about
  const partId = typeof step.input.part_id === "number" ? step.input.part_id : null;
  const partAt = typeof step.input.at_hour === "number" ? step.input.at_hour : hour;
  const result = step.result;

  let shown = result?.preview ?? "";
  try {
    shown = JSON.stringify(JSON.parse(shown), null, 2);
  } catch {
    // Not JSON (an error message, or a long result cut short): show as sent
  }

  return (
    <li className="tool-step">
      <div className="tool-row">
        <span className="tool-label">{TOOL_LABELS[step.name] ?? step.name}</span>
        <code className="tool-call">{call}</code>
        <span className="tool-status">
          {!result ? (
            <span className="muted small">running…</span>
          ) : result.is_error ? (
            <Badge tone="warning">Tool error</Badge>
          ) : (
            <span className="muted small">{formatBytes(result.chars)}</span>
          )}
        </span>
        {partId !== null && <a href={toHash({ page: "parts", partId, at: partAt })}>Part trace</a>}
        {result && (
          <button type="button" className="toggle" aria-expanded={open} onClick={() => setOpen((shown) => !shown)}>
            {open ? "Hide result" : "Show result"}
          </button>
        )}
      </div>
      {open && result && (
        <pre className="tool-result">
          {shown}
          {result.preview.length < result.chars &&
            `\n… the first ${formatInt(result.preview.length)} of ${formatInt(result.chars)} characters`}
        </pre>
      )}
    </li>
  );
}
