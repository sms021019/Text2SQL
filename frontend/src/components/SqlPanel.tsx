interface SqlPanelProps {
  sql: string;
  explanation: string;
  repaired: boolean;
  truncated: boolean;
  tables: string[];
}

export function SqlPanel({
  sql,
  explanation,
  repaired,
  truncated,
  tables,
}: SqlPanelProps) {
  return (
    <section className="sql-panel">
      <div className="badges">
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
