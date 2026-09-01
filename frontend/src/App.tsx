import { useEffect, useRef, useState } from "react";
import "./App.css";
import * as api from "./api";
import type { JobState, QueryResponse, SchemaResponse } from "./api";
import { QuestionForm, type SubmitOptions } from "./components/QuestionForm";
import { SqlPanel } from "./components/SqlPanel";
import { ResultsTable } from "./components/ResultsTable";
import { SchemaSidebar } from "./components/SchemaSidebar";
import { ErrorBanner } from "./components/ErrorBanner";
import { JobStatus } from "./components/JobStatus";

const POLL_INTERVAL_MS = 1000;
const MAX_POLL_ATTEMPTS = 300; // 5 minutes at a 1s interval

/** A job is only ever reported to the UI while it's pending -- see
 * `JobStatus`'s docstring. */
type PendingJobStatus = Extract<JobState, "queued" | "in_progress">;

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function errorMessage(err: unknown): string {
  return err instanceof api.ApiError ? err.detail : "request failed";
}

function App() {
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<PendingJobStatus | null>(null);

  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(true);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  // Guards against a stale async submission (a prior sync request, enqueue,
  // or poll) touching state after the user has moved on to a new question.
  // `abortRef` cancels whatever fetch is in flight; `generationRef` also
  // invalidates a poll that is merely *scheduled* (waiting on its 1s
  // `setTimeout`, no fetch to abort yet).
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function cancelPending() {
    generationRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }

  useEffect(() => cancelPending, []);

  useEffect(() => {
    const controller = new AbortController();
    api
      .getSchema(controller.signal)
      .then(setSchema)
      .catch((err: unknown) => {
        if (isAbortError(err)) return;
        setSchemaError(
          err instanceof api.ApiError ? err.detail : "failed to load schema",
        );
      })
      .finally(() => setSchemaLoading(false));
    return () => controller.abort();
  }, []);

  function schedulePoll(jobId: string, generation: number, attempt: number) {
    timeoutRef.current = setTimeout(() => {
      void pollJob(jobId, generation, attempt);
    }, POLL_INTERVAL_MS);
  }

  async function pollJob(jobId: string, generation: number, attempt: number) {
    if (generation !== generationRef.current) return;

    const controller = new AbortController();
    abortRef.current = controller;

    let status: api.JobStatusResponse;
    try {
      status = await api.getJob(jobId, controller.signal);
    } catch (err) {
      if (isAbortError(err)) return;
      if (generation !== generationRef.current) return;
      setJobStatus(null);
      setRequestError(errorMessage(err));
      return;
    }
    if (generation !== generationRef.current) return;

    if (status.status === "complete") {
      setJobStatus(null);
      setResult(status.result);
      return;
    }
    if (status.status === "failed") {
      setJobStatus(null);
      setRequestError(status.error ?? "job failed");
      return;
    }
    if (status.status === "not_found") {
      setJobStatus(null);
      setRequestError("job expired or unknown");
      return;
    }

    // queued | in_progress
    if (attempt >= MAX_POLL_ATTEMPTS) {
      setJobStatus(null);
      setRequestError("Timed out waiting for the job to finish after 5 minutes.");
      return;
    }
    setJobStatus(status.status);
    schedulePoll(jobId, generation, attempt + 1);
  }

  async function handleSubmit({ useCache, background }: SubmitOptions) {
    cancelPending();
    const generation = generationRef.current;

    setResult(null);
    setRequestError(null);
    setJobStatus(null);
    setLoading(true);

    const controller = new AbortController();
    abortRef.current = controller;

    if (background) {
      // `loading` only covers this initial POST -- once the job is
      // enqueued the form re-enables so a new question can be submitted
      // (which cancels this job's polling, see `cancelPending`). The
      // `JobStatus` pill is what signals the job is still pending.
      try {
        const enqueued = await api.askQuestionAsync(question, useCache, controller.signal);
        if (generation !== generationRef.current) return;
        setJobStatus(enqueued.status);
        schedulePoll(enqueued.job_id, generation, 1);
      } catch (err) {
        if (isAbortError(err)) return;
        if (generation !== generationRef.current) return;
        setRequestError(errorMessage(err));
      } finally {
        if (generation === generationRef.current) setLoading(false);
      }
      return;
    }

    try {
      const out = await api.askQuestion(question, useCache, controller.signal);
      if (generation !== generationRef.current) return;
      setResult(out);
    } catch (err) {
      if (isAbortError(err)) return;
      if (generation !== generationRef.current) return;
      setResult(null);
      setRequestError(errorMessage(err));
    } finally {
      if (generation === generationRef.current) setLoading(false);
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Text2SQL</h1>
      </header>
      <div className="app-body">
        <main className="app-main">
          <QuestionForm
            question={question}
            loading={loading}
            onQuestionChange={setQuestion}
            onSubmit={(opts) => void handleSubmit(opts)}
          />

          {jobStatus && <JobStatus status={jobStatus} />}
          {requestError && <ErrorBanner message={requestError} />}
          {result?.error && <ErrorBanner message={result.error} />}

          {result && (
            <>
              <SqlPanel
                sql={result.sql}
                explanation={result.explanation}
                repaired={result.repaired}
                truncated={result.truncated}
                tables={result.tables}
                cacheStatus={result.cache_status}
              />
              <ResultsTable
                columns={result.columns}
                rows={result.rows}
                rowCount={result.row_count}
              />
            </>
          )}
        </main>
        <SchemaSidebar schema={schema} loading={schemaLoading} error={schemaError} />
      </div>
      {result && (
        <footer className="app-footer">
          <div className="timings">
            {result.timings.map((t) => (
              <span key={t.stage} className="timing">
                {t.stage}: {t.ms.toFixed(0)}ms
              </span>
            ))}
          </div>
          <div className="usage">
            <span>
              tokens: {result.usage.prompt_tokens}+{result.usage.completion_tokens}
            </span>
            <span>latency: {result.usage.latency_ms.toFixed(0)}ms</span>
            <span className="request-id">request_id: {result.request_id}</span>
          </div>
        </footer>
      )}
    </div>
  );
}

export default App;
