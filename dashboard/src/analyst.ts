import { useSyncExternalStore } from "react";

import { ApiError, postEvents, type AnalystEvent } from "./api";

// ============================================================
// THE ANALYST CONVERSATION
//
// Kept outside the page, so an answer keeps streaming while another page
// is open. The server holds the conversation's history; this holds what
// the page shows.
// ============================================================

export interface ToolStep {
  kind: "tool";
  turn: number;
  id: string;
  name: string;
  input: Record<string, unknown>;
  result?: { is_error: boolean; chars: number; preview: string };
}

/** Text Claude wrote in a turn (the answer, or a note between tool calls) */
export interface TextStep {
  kind: "text";
  turn: number;
  text: string;
}

export type Step = ToolStep | TextStep;

export type Done = Extract<AnalystEvent, { event: "done" }>["data"];

export interface Exchange {
  question: string;
  steps: Step[];
  done?: Done;
  error?: string;
  stopped?: boolean;
}

export interface Conversation {
  /** Set by the server's first event */
  id: string | null;
  /** The hour the conversation answers as of; null is the end of the data */
  atHour: number | null;
  exchanges: Exchange[];
  running: boolean;
}

const EMPTY: Conversation = { id: null, atHour: null, exchanges: [], running: false };

let state = EMPTY;
let controller: AbortController | null = null;
// Bumped by newConversation(), so a stopped request can't write into the next conversation
let generation = 0;
const listeners = new Set<() => void>();

function set(next: Conversation) {
  state = next;
  listeners.forEach((listener) => listener());
}

function updateLast(change: (exchange: Exchange) => Exchange) {
  const exchanges = state.exchanges.slice();
  exchanges[exchanges.length - 1] = change(exchanges[exchanges.length - 1]);
  set({ ...state, exchanges });
}

function apply({ event, data }: AnalystEvent) {
  switch (event) {
    case "start":
      set({ ...state, id: data.conversation_id, atHour: data.at_hour });
      break;
    case "text":
      updateLast((exchange) => {
        const steps = exchange.steps.slice();
        const last = steps[steps.length - 1];
        if (last?.kind === "text" && last.turn === data.turn) steps[steps.length - 1] = { ...last, text: last.text + data.text };
        else steps.push({ kind: "text", turn: data.turn, text: data.text });
        return { ...exchange, steps };
      });
      break;
    case "tool_call":
      updateLast((exchange) => ({ ...exchange, steps: [...exchange.steps, { kind: "tool", ...data }] }));
      break;
    case "tool_result":
      updateLast((exchange) => ({
        ...exchange,
        steps: exchange.steps.map((step) =>
          step.kind === "tool" && step.id === data.id
            ? { ...step, result: { is_error: data.is_error, chars: data.chars, preview: data.preview } }
            : step,
        ),
      }));
      break;
    case "done":
      updateLast((exchange) => ({ ...exchange, done: data }));
      break;
    case "error":
      updateLast((exchange) => ({ ...exchange, error: data.message }));
      break;
  }
}

/** Ask a question; the first one fixes the conversation's hour at `at`. */
export async function ask(question: string, at: number | null) {
  if (state.running) return;

  const atHour = state.exchanges.length === 0 ? at : state.atHour;
  const conversationId = state.id;
  set({ ...state, atHour, running: true, exchanges: [...state.exchanges, { question, steps: [] }] });

  const current = new AbortController();
  const mine = generation;
  controller = current;

  try {
    const onEvent = (event: AnalystEvent) => {
      if (generation === mine) apply(event);
    };
    await postEvents("/analyst/ask", { question, at_hour: atHour, conversation_id: conversationId }, onEvent, current.signal);
  } catch (error) {
    if (generation !== mine) return;
    if (current.signal.aborted) {
      updateLast((exchange) => ({ ...exchange, stopped: true }));
    } else {
      const message = error instanceof ApiError ? error.message : `Couldn't reach the analyst: ${String(error)}`;
      updateLast((exchange) => ({ ...exchange, error: message }));
    }
  } finally {
    if (controller === current) controller = null;
  }

  if (generation === mine) {
    const last = state.exchanges[state.exchanges.length - 1];
    if (last && !last.done && !last.error && !last.stopped) {
      updateLast((exchange) => ({ ...exchange, error: "The answer ended early: the connection to the API closed." }));
    }
    set({ ...state, running: false });
  }
}

/** Stop the answer in progress; the server forgets the question. */
export function stop() {
  controller?.abort();
}

export function newConversation() {
  generation += 1;
  controller?.abort();
  controller = null;
  set(EMPTY);
}

export function useConversation(): Conversation {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
  );
}
