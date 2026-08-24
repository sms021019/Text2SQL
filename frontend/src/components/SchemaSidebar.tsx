import { useState } from "react";
import type { SchemaResponse, SchemaTable } from "../api";

interface SchemaSidebarProps {
  schema: SchemaResponse | null;
  loading: boolean;
  error: string | null;
}

function TableEntry({ table }: { table: SchemaTable }) {
  const [open, setOpen] = useState(false);

  return (
    <li className="schema-table">
      <button
        type="button"
        className="schema-table-toggle"
        onClick={() => setOpen((o) => !o)}
        title={table.comment ?? undefined}
      >
        <span className={`disclosure ${open ? "open" : ""}`}>&#9656;</span>
        {table.name}
      </button>
      {open && (
        <ul className="schema-columns">
          {table.columns.map((col) => (
            <li
              key={col.name}
              className="schema-column"
              title={col.comment ?? undefined}
            >
              {col.is_pk && <span className="pk-marker">PK</span>}
              <span className="column-name">{col.name}</span>
              <span className="column-type">
                {col.type}
                {col.nullable ? "?" : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function SchemaSidebar({ schema, loading, error }: SchemaSidebarProps) {
  return (
    <aside className="schema-sidebar">
      <h2>Schema</h2>
      {loading && <p className="schema-status">Loading schema…</p>}
      {error && <p className="schema-status schema-error">{error}</p>}
      {schema && (
        <>
          <p className="schema-version">version {schema.version}</p>
          <ul className="schema-tables">
            {schema.tables.map((table) => (
              <TableEntry key={table.name} table={table} />
            ))}
          </ul>
        </>
      )}
    </aside>
  );
}
