import { useEffect, useState } from "react";
import "./App.css";
import * as api from "./api";
import type { QueryResponse, SchemaResponse } from "./api";
import { QuestionForm } from "./components/QuestionForm";
import { SqlPanel } from "./components/SqlPanel";
import { ResultsTable } from "./components/ResultsTable";
import { SchemaSidebar } from "./components/SchemaSidebar";
import { ErrorBanner } from "./components/ErrorBanner";

function App() {
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);

  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(true);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    api
      .getSchema(controller.signal)
      .then(setSchema)
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setSchemaError(
          err instanceof api.ApiError ? err.detail : "failed to load schema",
        );
      })
      .finally(() => setSchemaLoading(false));
    return () => controller.abort();
  }, []);

  async function handleSubmit() {
    setLoading(true);
    setRequestError(null);
    try {
      const out = await api.askQuestion(question);
      setResult(out);
    } catch (err) {
      setResult(null);
      setRequestError(
        err instanceof api.ApiError ? err.detail : "request failed",
      );
    } finally {
      setLoading(false);
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
            onSubmit={handleSubmit}
          />

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
