import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Card, CardDescription, CardTitle } from "../components/ui/card";
import { StatusBadge } from "../components/ui/badge";
import {
  getInvestigation,
  type Hypothesis,
  type InvestigationDetail,
} from "../lib/api";

const POLL_MS = 2000;

function isActiveStatus(status: string) {
  return status === "pending" || status === "investigating";
}

function rankedHypotheses(hypotheses: Hypothesis[]) {
  return [...hypotheses].sort(
    (a, b) => b.confidence_score - a.confidence_score,
  );
}

export function DetailPage() {
  const { id } = useParams<{ id: string }>();
  const [investigation, setInvestigation] = useState<InvestigationDetail | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;

    let cancelled = false;
    let timer: number | undefined;

    async function load() {
      try {
        const detail = await getInvestigation(id!);
        if (cancelled) return;
        setInvestigation(detail);
        setError(null);
        if (isActiveStatus(detail.status)) {
          timer = window.setTimeout(load, POLL_MS);
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load investigation");
        timer = window.setTimeout(load, POLL_MS);
      }
    }

    void load();

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [id]);

  const hypotheses = useMemo(
    () => rankedHypotheses(investigation?.hypotheses ?? []),
    [investigation?.hypotheses],
  );

  if (error && !investigation) {
    return (
      <Card>
        <CardTitle>Investigation unavailable</CardTitle>
        <CardDescription>{error}</CardDescription>
        <div className="mt-4">
          <Link
            to="/history"
            className="inline-flex items-center justify-center rounded-md border border-[var(--border)] px-4 py-2 text-sm font-medium hover:bg-white/70"
          >
            Back to history
          </Link>
        </div>
      </Card>
    );
  }

  if (!investigation) {
    return (
      <Card>
        <CardTitle>Loading investigation…</CardTitle>
        <CardDescription>Fetching the latest status.</CardDescription>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle>Investigation detail</CardTitle>
            <CardDescription className="mt-2 max-w-3xl">
              {investigation.issue_description}
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <StatusBadge status={investigation.status} />
            {isActiveStatus(investigation.status) ? (
              <span className="text-xs text-[var(--muted)]">Auto-refreshing…</span>
            ) : null}
          </div>
        </div>
        <p className="mt-3 text-xs text-[var(--muted)]">
          id: {investigation.id}
        </p>
      </Card>

      <Card>
        <CardTitle>Final root cause</CardTitle>
        <p className="mt-3 text-sm leading-relaxed">
          {investigation.final_root_cause ?? (
            <span className="text-[var(--muted)]">
              {isActiveStatus(investigation.status)
                ? "Still investigating — root cause will appear when the run finishes."
                : "No root cause asserted (needs human review)."}
            </span>
          )}
        </p>
      </Card>

      <Card>
        <CardTitle>Hypotheses</CardTitle>
        <CardDescription>
          Ranked by confidence score (highest first).
        </CardDescription>
        {hypotheses.length === 0 ? (
          <p className="mt-3 text-sm text-[var(--muted)]">No hypotheses yet.</p>
        ) : (
          <ol className="mt-4 m-0 list-decimal space-y-3 pl-5">
            {hypotheses.map((hypothesis, index) => (
              <li key={`${hypothesis.description}-${index}`} className="text-sm">
                <div className="font-medium">
                  {hypothesis.description}
                </div>
                <div className="mt-1 text-xs text-[var(--muted)]">
                  confidence {hypothesis.confidence_score.toFixed(2)}
                  {hypothesis.supporting_evidence?.length
                    ? ` · evidence: ${hypothesis.supporting_evidence.join(", ")}`
                    : null}
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>

      <Card>
        <CardTitle>Evidence</CardTitle>
        <CardDescription>
          {investigation.evidence.length === 1
            ? "1 finding gathered so far."
            : `${investigation.evidence.length} findings gathered so far.`}
        </CardDescription>
        {investigation.evidence.length === 0 ? (
          <p className="mt-3 text-sm text-[var(--muted)]">No evidence yet.</p>
        ) : (
          <ul className="mt-4 m-0 list-none space-y-3 p-0">
            {investigation.evidence.map((item, index) => (
              <li
                key={`${item.source}-${index}`}
                className="rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
              >
                <div className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                  {item.source} · confidence {item.confidence.toFixed(2)}
                </div>
                <p className="m-0 mt-1 text-sm leading-relaxed">{item.finding}</p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="flex gap-2">
        <Link
          to="/history"
          className="inline-flex items-center justify-center rounded-md border border-[var(--border)] bg-transparent px-4 py-2 text-sm font-medium hover:bg-white/70"
        >
          History
        </Link>
        <Link
          to="/"
          className="inline-flex items-center justify-center rounded-md border border-[var(--border)] bg-transparent px-4 py-2 text-sm font-medium hover:bg-white/70"
        >
          New investigation
        </Link>
      </div>
    </div>
  );
}
