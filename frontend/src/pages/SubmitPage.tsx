import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../components/ui/button";
import { Card, CardDescription, CardTitle } from "../components/ui/card";
import { Textarea } from "../components/ui/textarea";
import { createInvestigation } from "../lib/api";

export function SubmitPage() {
  const navigate = useNavigate();
  const [issueDescription, setIssueDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = issueDescription.trim();
    if (!trimmed) {
      setError("Enter an issue description.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const created = await createInvestigation(trimmed);
      navigate(`/investigations/${created.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start investigation");
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl">
      <Card>
        <div className="mb-1 inline-flex items-center rounded-md bg-[var(--accent-muted)] px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-[var(--accent)]">
          New investigation
        </div>
        <CardTitle className="mt-3 text-xl sm:text-2xl">
          Describe the data issue
        </CardTitle>
        <CardDescription className="max-w-lg">
          Specialists gather lineage, SQL, warehouse, ETL, and schema evidence,
          then propose a validated root cause.
        </CardDescription>

        <form className="mt-6 space-y-5" onSubmit={onSubmit}>
          <label className="block space-y-2">
            <span className="text-sm font-semibold text-[var(--foreground)]">
              Issue description
            </span>
            <Textarea
              value={issueDescription}
              onChange={(event) => setIssueDescription(event.target.value)}
              placeholder="e.g. Total revenue for 2024-01-20 looks lower than expected on the daily dashboard…"
              disabled={submitting}
              aria-invalid={Boolean(error)}
            />
          </label>

          {error ? (
            <div
              className="rounded-[10px] border border-rose-200 bg-[var(--danger-muted)] px-3.5 py-2.5 text-sm text-[var(--danger)]"
              role="alert"
            >
              {error}
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-3 pt-1">
            <Button type="submit" disabled={submitting}>
              {submitting ? (
                <>
                  <span
                    className="size-3.5 animate-pulse-soft rounded-full bg-white/80"
                    aria-hidden
                  />
                  Starting…
                </>
              ) : (
                "Start investigation"
              )}
            </Button>
            <p className="m-0 text-xs text-[var(--muted-soft)]">
              You’ll be taken to the live detail view while agents run.
            </p>
          </div>
        </form>
      </Card>
    </div>
  );
}
