import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import * as api from "./api";
import { ApiError } from "./api";
import type { CacheStatus, JobStatusResponse } from "./api";

const SCHEMA_RESPONSE: api.SchemaResponse = {
  version: "v1",
  tables: [
    {
      name: "orders",
      comment: "order rows",
      columns: [
        {
          name: "id",
          type: "integer",
          nullable: false,
          comment: null,
          is_pk: true,
        },
      ],
    },
  ],
};

const QUERY_RESPONSE: api.QueryResponse = {
  sql: "SELECT count(*) FROM orders",
  explanation: "Counts all orders.",
  columns: ["count"],
  rows: [[42]],
  row_count: 1,
  truncated: false,
  tables: ["orders"],
  repaired: false,
  timings: [{ stage: "llm", ms: 123.4 }],
  usage: { prompt_tokens: 10, completion_tokens: 5, latency_ms: 200 },
  error: null,
  request_id: "req-abc",
  cache_status: "miss",
};

function jobStatusResponse(
  overrides: Partial<JobStatusResponse>,
): JobStatusResponse {
  return { job_id: "job-1", status: "queued", result: null, error: null, ...overrides };
}

/** Under `vi.useFakeTimers()`, user-event's own interactions (`type`,
 * `click`, ...) internally schedule a real zero-delay timer that isn't
 * routed through the `advanceTimers` option, so awaiting them directly
 * deadlocks. Racing a 0ms fake-timer advance alongside the interaction
 * drains that timer and lets it settle. */
async function withTimersPumped<T>(promise: Promise<T>): Promise<T> {
  const [result] = await Promise.all([promise, vi.advanceTimersByTimeAsync(0)]);
  return result;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("App", () => {
  it("submits a question and renders the SQL and results", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    vi.spyOn(api, "askQuestion").mockResolvedValue(QUERY_RESPONSE);
    const user = userEvent.setup();

    render(<App />);

    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await user.type(textarea, "How many orders are there?");
    await user.click(screen.getByRole("button", { name: /ask/i }));

    expect(await screen.findByText(/SELECT count\(\*\) FROM orders/)).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText(/Counts all orders\./)).toBeInTheDocument();
    expect(screen.getByText(/req-abc/)).toBeInTheDocument();
  });

  it("shows the error banner when the pipeline returns a 200 with an error field", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    vi.spyOn(api, "askQuestion").mockResolvedValue({
      ...QUERY_RESPONSE,
      sql: "",
      columns: [],
      rows: [],
      row_count: 0,
      error: "llm: request timed out",
    });
    const user = userEvent.setup();

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await user.type(textarea, "anything");
    await user.click(screen.getByRole("button", { name: /ask/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "llm: request timed out",
    );
  });

  it("collapses and expands the schema sidebar", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    const user = userEvent.setup();

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    expect(screen.getByRole("heading", { name: "Schema" })).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /collapse schema sidebar/i }),
    );
    expect(
      screen.queryByRole("heading", { name: "Schema" }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /expand schema sidebar/i }),
    );
    expect(screen.getByRole("heading", { name: "Schema" })).toBeInTheDocument();
  });

  it("shows the error banner when the request itself fails (ApiError)", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    vi.spyOn(api, "askQuestion").mockRejectedValue(
      new ApiError(503, "schema not loaded"),
    );
    const user = userEvent.setup();

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await user.type(textarea, "anything");
    await user.click(screen.getByRole("button", { name: /ask/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "schema not loaded",
    );
  });

  it("has 'Use cache' checked and 'Run in background' unchecked by default", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    expect(screen.getByRole("checkbox", { name: /use cache/i })).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: /run in background/i }),
    ).not.toBeChecked();
  });

  it("sends use_cache: false on the sync path when the checkbox is unchecked", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    const askQuestionMock = vi
      .spyOn(api, "askQuestion")
      .mockResolvedValue(QUERY_RESPONSE);
    const user = userEvent.setup();

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await user.type(textarea, "anything");
    await user.click(screen.getByRole("checkbox", { name: /use cache/i }));
    await user.click(screen.getByRole("button", { name: /ask/i }));

    expect(askQuestionMock).toHaveBeenCalledWith(
      "anything",
      false,
      expect.anything(),
    );
  });

  it.each([
    ["miss", "cache: miss"],
    ["sql_hit", "cache: sql hit"],
    ["result_hit", "cache: result hit"],
    ["bypass", "cache: bypass"],
    ["disabled", "cache: off"],
  ] satisfies [CacheStatus, string][])(
    "renders the cache badge for cache_status=%s",
    async (status, label) => {
      vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
      vi.spyOn(api, "askQuestion").mockResolvedValue({
        ...QUERY_RESPONSE,
        cache_status: status,
      });
      const user = userEvent.setup();

      render(<App />);
      await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

      const textarea = screen.getByPlaceholderText(/ask a question/i);
      await user.type(textarea, "anything");
      await user.click(screen.getByRole("button", { name: /ask/i }));

      expect(await screen.findByText(label)).toBeInTheDocument();
    },
  );

  it("runs in background: polls the job and renders the result once complete", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    vi.spyOn(api, "askQuestionAsync").mockResolvedValue({
      job_id: "job-1",
      status: "queued",
    });
    const getJobMock = vi
      .spyOn(api, "getJob")
      .mockResolvedValueOnce(jobStatusResponse({ status: "queued" }))
      .mockResolvedValueOnce(jobStatusResponse({ status: "in_progress" }))
      .mockResolvedValueOnce(
        jobStatusResponse({ status: "complete", result: QUERY_RESPONSE }),
      );

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    vi.useFakeTimers();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await withTimersPumped(user.type(textarea, "How many orders are there?"));
    await withTimersPumped(
      user.click(screen.getByRole("checkbox", { name: /run in background/i })),
    );
    await withTimersPumped(user.click(screen.getByRole("button", { name: /ask/i })));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Queued…")).toBeInTheDocument();
    expect(api.askQuestionAsync).toHaveBeenCalledWith(
      "How many orders are there?",
      true,
      expect.anything(),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("Queued…")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("Running…")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(
      screen.getByText(/SELECT count\(\*\) FROM orders/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Queued…")).not.toBeInTheDocument();
    expect(screen.queryByText("Running…")).not.toBeInTheDocument();
    expect(getJobMock).toHaveBeenCalledTimes(3);
  });

  it("shows the error banner when the async enqueue fails with a 503 (queue unavailable)", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    vi.spyOn(api, "askQuestionAsync").mockRejectedValue(
      new ApiError(503, "job queue unavailable"),
    );
    const getJobMock = vi.spyOn(api, "getJob");
    const user = userEvent.setup();

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await user.type(textarea, "anything");
    await user.click(screen.getByRole("checkbox", { name: /run in background/i }));
    await user.click(screen.getByRole("button", { name: /ask/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "job queue unavailable",
    );
    expect(screen.queryByText("Queued…")).not.toBeInTheDocument();
    expect(getJobMock).not.toHaveBeenCalled();
  });

  it("shows the job's error when a background job fails", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    vi.spyOn(api, "askQuestionAsync").mockResolvedValue({
      job_id: "job-1",
      status: "queued",
    });
    vi.spyOn(api, "getJob").mockResolvedValueOnce(
      jobStatusResponse({ status: "failed", error: "worker exploded" }),
    );

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    vi.useFakeTimers();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await withTimersPumped(user.type(textarea, "anything"));
    await withTimersPumped(
      user.click(screen.getByRole("checkbox", { name: /run in background/i })),
    );
    await withTimersPumped(user.click(screen.getByRole("button", { name: /ask/i })));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(screen.getByRole("alert")).toHaveTextContent("worker exploded");
    expect(screen.queryByText("Queued…")).not.toBeInTheDocument();
  });

  it("treats a not_found job as an error and stops polling", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    vi.spyOn(api, "askQuestionAsync").mockResolvedValue({
      job_id: "job-1",
      status: "queued",
    });
    const getJobMock = vi
      .spyOn(api, "getJob")
      .mockResolvedValue(jobStatusResponse({ status: "not_found" }));

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    vi.useFakeTimers();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await withTimersPumped(user.type(textarea, "anything"));
    await withTimersPumped(
      user.click(screen.getByRole("checkbox", { name: /run in background/i })),
    );
    await withTimersPumped(user.click(screen.getByRole("button", { name: /ask/i })));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(screen.getByRole("alert")).toHaveTextContent("job expired or unknown");
    expect(getJobMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(getJobMock).toHaveBeenCalledTimes(1);
  });

  it("gives up after 5 minutes of polling with a clear error message", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    vi.spyOn(api, "askQuestionAsync").mockResolvedValue({
      job_id: "job-1",
      status: "queued",
    });
    const getJobMock = vi
      .spyOn(api, "getJob")
      .mockResolvedValue(jobStatusResponse({ status: "queued" }));

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    vi.useFakeTimers();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await withTimersPumped(user.type(textarea, "anything"));
    await withTimersPumped(
      user.click(screen.getByRole("checkbox", { name: /run in background/i })),
    );
    await withTimersPumped(user.click(screen.getByRole("button", { name: /ask/i })));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    });

    expect(screen.getByRole("alert")).toHaveTextContent(/5 minutes/i);
    expect(getJobMock).toHaveBeenCalledTimes(300);
    expect(screen.queryByText("Queued…")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(getJobMock).toHaveBeenCalledTimes(300);
  }, 20000);

  it("cancels the previous job's polling when a new question is submitted", async () => {
    vi.spyOn(api, "getSchema").mockResolvedValue(SCHEMA_RESPONSE);
    vi.spyOn(api, "askQuestionAsync")
      .mockResolvedValueOnce({ job_id: "job-1", status: "queued" })
      .mockResolvedValueOnce({ job_id: "job-2", status: "queued" });
    // job-1's poll must never actually fire (it's cancelled before its 1s
    // timeout elapses), so only job-2's response is ever consumed.
    const getJobMock = vi.spyOn(api, "getJob").mockResolvedValueOnce(
      jobStatusResponse({
        job_id: "job-2",
        status: "complete",
        result: { ...QUERY_RESPONSE, sql: "SELECT 2" },
      }),
    );

    render(<App />);
    await waitFor(() => expect(api.getSchema).toHaveBeenCalled());

    vi.useFakeTimers();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const textarea = screen.getByPlaceholderText(/ask a question/i);
    await withTimersPumped(user.type(textarea, "first question"));
    await withTimersPumped(
      user.click(screen.getByRole("checkbox", { name: /run in background/i })),
    );
    await withTimersPumped(user.click(screen.getByRole("button", { name: /ask/i })));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Queued…")).toBeInTheDocument();

    // Resubmit before job-1's poll has fired -- must cancel it.
    await withTimersPumped(user.clear(textarea));
    await withTimersPumped(user.type(textarea, "second question"));
    await withTimersPumped(user.click(screen.getByRole("button", { name: /ask/i })));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(screen.getByText(/SELECT 2/)).toBeInTheDocument();
    expect(getJobMock).toHaveBeenCalledTimes(1);
    expect(getJobMock).toHaveBeenCalledWith("job-2", expect.anything());
  });
});
