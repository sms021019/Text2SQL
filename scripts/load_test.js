// k6 load test for POST /api/v1/query -- the traffic generator behind
// `make load-test`.
//
// Run:  docker run --rm -i --network host grafana/k6:0.55.0 run - <scripts/load_test.js
// or:   API_URL=http://host.docker.internal:8000 make load-test   (Docker Desktop)
//
// The questions below are copied verbatim from `seed/questions.yaml` (ids
// q01, q03, q05, q09, q10, q17, q19, q21, q22, q27 -- a spread of
// single-table lookups, joins, aggregates and a date filter). They are
// inlined rather than read from the YAML because k6 has no YAML parser and
// a generated JSON side-file would be one more thing to keep in sync.
import http from "k6/http";
import { check, sleep } from "k6";

// Overridable because `--network host` reaches the published 127.0.0.1
// ports on Linux, but not on Docker Desktop for macOS/Windows, where the
// k6 container needs http://host.docker.internal:8000 instead.
const API_URL = __ENV.API_URL || "http://127.0.0.1:8000";

const QUESTIONS = [
  "How many customers do we have?",
  "Show all suppliers based in Germany.",
  "How many products have been discontinued?",
  "List the products supplied by supplier 1, with their category name.",
  "For each order, show the customer's full name and country.",
  "List products that currently have stock below their reorder level.",
  "What is the total revenue (using order_items, not the product catalog price)?",
  "What are the top 5 products by revenue?",
  "What are the top 10 customers by number of orders placed?",
  "How many orders were placed in July 2026?",
];

export const options = {
  vus: 5,
  duration: "60s",
  thresholds: {
    // Transport-level only: the API answers 200 even when the pipeline
    // itself failed (the message is in the response's `error` field), so
    // this asserts the service stayed up, not that the LLM produced good
    // SQL. See the `pipeline ok` check below for the latter.
    http_req_failed: ["rate<0.01"],
  },
};

export default function () {
  // Walk the list rather than picking at random, so every question is asked
  // repeatedly within the run -- a cache hit needs the *same* question
  // twice.
  const question = QUESTIONS[__ITER % QUESTIONS.length];
  // Alternate the cache on and off per iteration so the Grafana cache
  // hit-rate panel has both hits and bypasses to plot.
  const useCache = __ITER % 2 === 0;

  const res = http.post(
    `${API_URL}/api/v1/query`,
    JSON.stringify({ question: question, use_cache: useCache }),
    {
      headers: { "Content-Type": "application/json" },
      tags: { name: "POST /api/v1/query" },
    },
  );

  check(res, {
    "status is 200": (r) => r.status === 200,
    "pipeline ok": (r) => {
      try {
        const body = r.json();
        return body !== null && body.error === null;
      } catch (e) {
        return false;
      }
    },
  });

  sleep(1);
}
