import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import * as api from "./api";
import { ApiError } from "./api";

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
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
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
});
