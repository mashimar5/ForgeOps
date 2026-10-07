import { ApiError } from "../api";

/** A short inline message for a request that failed */
export function ErrorText({ error }: { error: Error }) {
  return <p className="error-text">{error instanceof ApiError ? error.message : `Couldn't load this: ${error.message}`}</p>;
}

/** Stands in for a chart until its first data arrives */
export function Placeholder({ error, height = 240 }: { error?: Error; height?: number }) {
  return (
    <div className="placeholder" style={{ height }}>
      {error ? <ErrorText error={error} /> : <span>Loading…</span>}
    </div>
  );
}

/** The API isn't answering: say how to start it */
export function ApiDown({ error }: { error: Error }) {
  return (
    <div className="api-down">
      <h1>Can't reach the ForgeOps API</h1>
      <p>
        The dashboard reads everything from the API. Start it from the project root, then reload this page:
      </p>
      <pre>.venv/bin/uvicorn api:app --app-dir src</pre>
      <p className="muted">({error.message})</p>
    </div>
  );
}
