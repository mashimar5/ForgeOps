import { useState, type ReactNode } from "react";

export interface Column<T> {
  key: string;
  label: string;
  render: (row: T) => ReactNode;
  /** Numbers align right, in tabular figures */
  numeric?: boolean;
  /** Given for sortable columns */
  sortValue?: (row: T) => number | string | null;
}

interface TableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  caption: string;
  initialSort?: { key: string; descending: boolean };
  empty?: ReactNode;
}

export function Table<T>({ columns, rows, rowKey, caption, initialSort, empty }: TableProps<T>) {
  const [sort, setSort] = useState(initialSort);

  if (rows.length === 0) {
    return <div className="empty">{empty ?? "Nothing to show."}</div>;
  }

  const sortColumn = columns.find((column) => column.key === sort?.key);
  const sorted = sortColumn?.sortValue && sort ? sortRows(rows, sortColumn.sortValue, sort.descending) : rows;

  return (
    <div className="table-wrap">
      <table>
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => {
              const active = sort?.key === column.key;
              return (
                <th
                  key={column.key}
                  scope="col"
                  className={column.numeric ? "num" : undefined}
                  aria-sort={active ? (sort.descending ? "descending" : "ascending") : undefined}
                >
                  {column.sortValue ? (
                    <button
                      type="button"
                      className="sort"
                      onClick={() => setSort({ key: column.key, descending: active ? !sort.descending : true })}
                    >
                      {column.label}
                      <span aria-hidden="true">{active ? (sort.descending ? " ↓" : " ↑") : ""}</span>
                    </button>
                  ) : (
                    column.label
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((column) => (
                <td key={column.key} className={column.numeric ? "num" : undefined}>
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Sort a copy; missing values always go last. */
function sortRows<T>(rows: T[], value: (row: T) => number | string | null, descending: boolean): T[] {
  return [...rows].sort((a, b) => {
    const x = value(a);
    const y = value(b);
    if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1;
    const order = x < y ? -1 : x > y ? 1 : 0;
    return descending ? -order : order;
  });
}
