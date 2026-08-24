const MAX_ROWS = 500;

interface ResultsTableProps {
  columns: string[];
  rows: unknown[][];
  rowCount: number;
}

function renderCell(value: unknown): string {
  if (value === null || value === undefined) return "∅";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function ResultsTable({ columns, rows, rowCount }: ResultsTableProps) {
  if (columns.length === 0) {
    return <p className="results-empty">No results.</p>;
  }

  const shown = rows.slice(0, MAX_ROWS);

  return (
    <section className="results-table-wrap">
      <div className="results-meta">
        {rowCount} row{rowCount === 1 ? "" : "s"}
        {shown.length < rowCount && ` (showing first ${shown.length})`}
      </div>
      <div className="results-scroll">
        <table className="results-table">
          <thead>
            <tr>
              {columns.map((col) => (
                <th key={col}>{col}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j}>{renderCell(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
