import type { CacheStatus } from "../api";

/** The one place mapping every `CacheStatus` value to its badge text. */
const CACHE_STATUS_LABELS: Record<CacheStatus, string> = {
  miss: "cache: miss",
  sql_hit: "cache: sql hit",
  result_hit: "cache: result hit",
  bypass: "cache: bypass",
  disabled: "cache: off",
};

interface SqlPanelProps {
  sql: string;
  explanation: string;
  repaired: boolean;
  truncated: boolean;
  tables: string[];
  cacheStatus: CacheStatus;
}

export function SqlPanel({
  sql,
  explanation,
  repaired,
  truncated,
  tables,
  cacheStatus,
}: SqlPanelProps) {
  return (
    <section className="sql-panel">
      <div className="badges">
        <span className="badge badge-cache">{CACHE_STATUS_LABELS[cacheStatus]}</span>
        {repaired && <span className="badge badge-warn">repaired</span>}
        {truncated && <span className="badge badge-warn">truncated</span>}
        {tables.map((t) => (
          <span key={t} className="badge badge-table">
            {t}
          </span>
        ))}
      </div>
      <pre className="sql-code">
        <code>{sql}</code>
      </pre>
      {explanation && <p className="explanation">{explanation}</p>}
    </section>
  );
}
