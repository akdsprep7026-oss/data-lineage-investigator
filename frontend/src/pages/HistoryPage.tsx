import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardDescription, CardTitle } from "../components/ui/card";
import { StatusBadge } from "../components/ui/badge";
import { listInvestigations, type InvestigationSummary } from "../lib/api";

function formatWhen(value: string) {
  return new Date(value).toLocaleString();
}

export function HistoryPage() {
  const [items, setItems] = useState<InvestigationSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data = await listInvestigations();
        if (!cancelled) {
          setItems(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load history");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Card>
      <CardTitle>History</CardTitle>
      <CardDescription>
        Past investigations with status and root cause.
      </CardDescription>

      {loading ? (
        <p className="mt-4 text-sm text-[var(--muted)]">Loading…</p>
      ) : null}
      {error ? (
        <p className="mt-4 text-sm text-[var(--danger)]" role="alert">
          {error}
        </p>
      ) : null}

      {!loading && !error && items.length === 0 ? (
        <p className="mt-4 text-sm text-[var(--muted)]">
          No investigations yet.{" "}
          <Link to="/" className="underline">
            Submit one
          </Link>
          .
        </p>
      ) : null}

      {items.length > 0 ? (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[40rem] border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wide text-[var(--muted)]">
                <th className="px-2 py-2 font-medium">Created</th>
                <th className="px-2 py-2 font-medium">Status</th>
                <th className="px-2 py-2 font-medium">Issue</th>
                <th className="px-2 py-2 font-medium">Root cause</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr
                  key={item.id}
                  className="border-b border-[var(--border)] align-top last:border-b-0"
                >
                  <td className="whitespace-nowrap px-2 py-3 text-[var(--muted)]">
                    {formatWhen(item.created_at)}
                  </td>
                  <td className="px-2 py-3">
                    <StatusBadge status={item.status} />
                  </td>
                  <td className="px-2 py-3">
                    <Link
                      to={`/investigations/${item.id}`}
                      className="font-medium underline-offset-2 hover:underline"
                    >
                      {item.issue_description}
                    </Link>
                  </td>
                  <td className="px-2 py-3 text-[var(--muted)]">
                    {item.final_root_cause ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Card>
  );
}
