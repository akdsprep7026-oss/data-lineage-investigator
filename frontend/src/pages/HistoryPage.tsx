import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardDescription, CardTitle } from "../components/ui/card";
import { StatusBadge } from "../components/ui/badge";
import { listInvestigations, type InvestigationSummary } from "../lib/api";

function formatWhen(value: string) {
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function truncate(text: string, max: number) {
  if (text.length <= max) return text;
  return `${text.slice(0, max).trimEnd()}…`;
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
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="m-0 text-xl font-semibold tracking-tight sm:text-2xl">
            History
          </h2>
          <p className="m-0 mt-1 text-sm text-[var(--muted)]">
            Past investigations with status and root cause.
          </p>
        </div>
        <Link
          to="/"
          className="inline-flex items-center justify-center rounded-[10px] border border-[var(--border)] bg-white px-3.5 py-2 text-sm font-semibold shadow-sm transition hover:border-[var(--border-strong)]"
        >
          New investigation
        </Link>
      </div>

      {loading ? (
        <Card>
          <div className="flex items-center gap-3">
            <span className="size-2 animate-pulse-soft rounded-full bg-[var(--accent)]" />
            <p className="m-0 text-sm text-[var(--muted)]">Loading history…</p>
          </div>
        </Card>
      ) : null}

      {error ? (
        <Card className="border-rose-200 bg-[var(--danger-muted)]">
          <CardTitle className="text-[var(--danger)]">Could not load history</CardTitle>
          <CardDescription className="text-[var(--danger)]/80">{error}</CardDescription>
        </Card>
      ) : null}

      {!loading && !error && items.length === 0 ? (
        <Card className="border-dashed py-12 text-center">
          <div className="mx-auto flex size-10 items-center justify-center rounded-full bg-[var(--accent-muted)] text-[var(--accent)]">
            <span className="font-[family-name:var(--font-mono)] text-sm font-medium">
              0
            </span>
          </div>
          <CardTitle className="mt-4">No investigations yet</CardTitle>
          <CardDescription className="mx-auto mt-2 max-w-sm">
            Submit a data issue to start the multi-agent workflow. Results will
            appear here once created.
          </CardDescription>
          <div className="mt-5">
            <Link
              to="/"
              className="inline-flex items-center justify-center rounded-[10px] bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-[var(--accent-hover)]"
            >
              Start an investigation
            </Link>
          </div>
        </Card>
      ) : null}

      {items.length > 0 ? (
        <>
          {/* Desktop / tablet table */}
          <Card className="hidden overflow-hidden p-0 md:block">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[40rem] border-collapse text-left text-sm">
                <thead>
                  <tr className="border-b border-[var(--border)] bg-[var(--background-elevated)] text-[11px] uppercase tracking-[0.08em] text-[var(--muted)]">
                    <th className="px-5 py-3 font-semibold">Created</th>
                    <th className="px-5 py-3 font-semibold">Status</th>
                    <th className="px-5 py-3 font-semibold">Issue</th>
                    <th className="px-5 py-3 font-semibold">Root cause</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr
                      key={item.id}
                      className="border-b border-[var(--border)] align-top transition last:border-b-0 hover:bg-[var(--background-elevated)]/80"
                    >
                      <td className="whitespace-nowrap px-5 py-4 font-[family-name:var(--font-mono)] text-xs text-[var(--muted)]">
                        {formatWhen(item.created_at)}
                      </td>
                      <td className="px-5 py-4">
                        <StatusBadge status={item.status} />
                      </td>
                      <td className="px-5 py-4">
                        <Link
                          to={`/investigations/${item.id}`}
                          className="font-medium text-[var(--foreground)] underline-offset-2 hover:underline"
                        >
                          {truncate(item.issue_description, 120)}
                        </Link>
                      </td>
                      <td className="max-w-xs px-5 py-4 text-[var(--muted)]">
                        {item.final_root_cause
                          ? truncate(item.final_root_cause, 100)
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          {/* Mobile cards */}
          <div className="space-y-3 md:hidden">
            {items.map((item) => (
              <Link key={item.id} to={`/investigations/${item.id}`} className="block">
                <Card className="transition hover:border-[var(--border-strong)]">
                  <div className="flex items-start justify-between gap-3">
                    <StatusBadge status={item.status} />
                    <span className="font-[family-name:var(--font-mono)] text-[11px] text-[var(--muted-soft)]">
                      {formatWhen(item.created_at)}
                    </span>
                  </div>
                  <p className="m-0 mt-3 text-sm font-medium leading-snug">
                    {item.issue_description}
                  </p>
                  <p className="m-0 mt-2 text-xs leading-relaxed text-[var(--muted)]">
                    {item.final_root_cause ?? "Root cause pending"}
                  </p>
                </Card>
              </Link>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
