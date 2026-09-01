/** Typed client for the Text2SQL backend's `/api/v1` endpoints.
 *
 * Mirrors `backend/app/api/v1/query.py` and `schema.py`'s pydantic
 * response models. `askQuestion` never throws on a pipeline error --
 * that surfaces as a non-null `error` field on a 200 response, per
 * `Text2SQLPipeline.run`'s contract -- but it does throw `ApiError` for
 * a non-2xx HTTP status (422 validation, 503 not-ready, etc).
 */

const BASE = import.meta.env.VITE_API_URL ?? "/api";

export interface Timing {
  stage: string;
  ms: number;
}

export interface Usage {
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
}

/** Mirrors `app.core.pipeline.CacheStatus`. */
export type CacheStatus = "miss" | "sql_hit" | "result_hit" | "bypass" | "disabled";

export interface QueryResponse {
  sql: string;
  explanation: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  tables: string[];
  repaired: boolean;
  timings: Timing[];
  usage: Usage;
  error: string | null;
  request_id: string;
  cache_status: CacheStatus;
}

/** Mirrors `app.api.v1.jobs.JobState`. */
export type JobState = "queued" | "in_progress" | "complete" | "failed" | "not_found";

export interface EnqueueResponse {
  job_id: string;
  status: "queued";
}

export interface JobStatusResponse {
  job_id: string;
  status: JobState;
  result: QueryResponse | null;
  error: string | null;
}

export interface SchemaColumn {
  name: string;
  type: string;
  nullable: boolean;
  comment: string | null;
  is_pk: boolean;
}

export interface SchemaTable {
  name: string;
  comment: string | null;
  columns: SchemaColumn[];
}

export interface SchemaResponse {
  version: string;
  tables: SchemaTable[];
}

/** Thrown for any non-2xx response. `detail` is FastAPI's parsed
 * `{detail}` body when present, else the raw response text. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API error ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function parseErrorDetail(res: Response): Promise<string> {
  const text = await res.text();
  try {
    const body = JSON.parse(text) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (body.detail !== undefined) return JSON.stringify(body.detail);
  } catch {
    // not JSON -- fall through to raw text
  }
  return text || res.statusText;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    throw new ApiError(res.status, await parseErrorDetail(res));
  }
  return (await res.json()) as T;
}

export function askQuestion(
  question: string,
  useCache = true,
  signal?: AbortSignal,
): Promise<QueryResponse> {
  return request<QueryResponse>("/v1/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, use_cache: useCache }),
    signal,
  });
}

/** Enqueues the question onto the background job queue. Throws `ApiError`
 * (503) when the job queue is unavailable, same as any other API error. */
export function askQuestionAsync(
  question: string,
  useCache = true,
  signal?: AbortSignal,
): Promise<EnqueueResponse> {
  return request<EnqueueResponse>("/v1/query/async", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, use_cache: useCache }),
    signal,
  });
}

export function getJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobStatusResponse> {
  return request<JobStatusResponse>(`/v1/jobs/${jobId}`, { signal });
}

export function getSchema(signal?: AbortSignal): Promise<SchemaResponse> {
  return request<SchemaResponse>("/v1/schema", { signal });
}
