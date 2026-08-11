import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Card, CardDescription, CardTitle } from "../components/ui/card";
import { StatusBadge } from "../components/ui/badge";
import {
  getInvestigation,
  type EvidenceEntry,
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

function formatWhen(value: string) {
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function ConfidenceBar({
  value,
  label,
}: {
  value: number;
  label?: string;
}) {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <div className="min-w-0 flex-1">
      {label ? (
        <div className="mb-1 flex items-center justify-between gap-2 text-xs">
          <span className="text-[var(--muted)]">{label}</span>
          <span className="font-[family-name:var(--font-mono)] font-medium tabular-nums text-[var(--foreground)]">
            {value.toFixed(2)}
          </span>
        </div>
      ) : null}
      <div className="h-1.5 overflow-hidden rounded-full bg-[var(--border)]">
        <div
          className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function groupEvidenceBySource(evidence: EvidenceEntry[]) {
  const groups = new Map<string, EvidenceEntry[]>();
  for (const item of evidence) {
    const key = item.source || "unknown";
    const list = groups.get(key);
    if (list) list.push(item);
    else groups.set(key, [item]);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
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

  const evidenceGroups = useMemo(
    () => groupEvidenceBySource(investigation?.evidence ?? []),
    [investigation?.evidence],
  );

  if (error && !investigation) {
    return (
      <Card className="border-rose-200">
        <CardTitle>Investigation unavailable</CardTitle>
        <CardDescription className="text-[var(--danger)]">{error}</CardDescription>
        <div className="mt-5">
          <Link
            to="/history"
            className="inline-flex items-center justify-center rounded-[10px] border border-[var(--border)] bg-white px-4 py-2 text-sm font-semibold shadow-sm hover:border-[var(--border-strong)]"
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
        <div className="flex items-center gap-3">
          <span className="size-2 animate-pulse-soft rounded-full bg-[var(--accent)]" />
          <div>
            <CardTitle>Loading investigation…</CardTitle>
            <CardDescription>Fetching the latest status.</CardDescription>
          </div>
        </div>
      </Card>
    );
  }

  const active = isActiveStatus(investigation.status);

  return (
    <div className="space-y-4 sm:space-y-5">
      {/* Header */}
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={investigation.status} />
              {active ? (
                <span className="inline-flex items-center gap-1.5 text-xs font-medium text-[var(--info)]">
                  <span className="size-1.5 animate-pulse-soft rounded-full bg-[var(--info)]" />
                  Auto-refreshing…
                </span>
              ) : null}
            </div>
            <h2 className="m-0 mt-3 text-lg font-semibold leading-snug tracking-tight sm:text-xl">
              {investigation.issue_description}
            </h2>
            <dl className="mt-4 grid gap-2 text-xs text-[var(--muted)] sm:grid-cols-2">
              <div className="flex gap-2">
                <dt className="shrink-0 font-medium text-[var(--muted-soft)]">
                  Created
                </dt>
                <dd className="m-0 font-[family-name:var(--font-mono)]">
                  {formatWhen(investigation.created_at)}
                </dd>
              </div>
              <div className="flex gap-2">
                <dt className="shrink-0 font-medium text-[var(--muted-soft)]">
                  Updated
                </dt>
                <dd className="m-0 font-[family-name:var(--font-mono)]">
                  {formatWhen(investigation.updated_at)}
                </dd>
              </div>
              <div className="flex min-w-0 gap-2 sm:col-span-2">
                <dt className="shrink-0 font-medium text-[var(--muted-soft)]">
                  ID
                </dt>
                <dd className="m-0 truncate font-[family-name:var(--font-mono)] text-[11px]">
                  {investigation.id}
                </dd>
              </div>
            </dl>
          </div>
        </div>
      </Card>

      {/* Active progress banner */}
      {active ? (
        <div className="flex items-start gap-3 rounded-[var(--radius)] border border-sky-200/80 bg-[var(--info-muted)] px-4 py-3.5 sm:px-5">
          <span className="mt-1.5 size-2 shrink-0 animate-pulse-soft rounded-full bg-[var(--info)]" />
          <div>
            <p className="m-0 text-sm font-semibold text-[var(--info)]">
              Investigation in progress
            </p>
            <p className="m-0 mt-0.5 text-sm text-[var(--muted)]">
              Agents are gathering evidence and ranking hypotheses. This page
              refreshes every few seconds.
            </p>
          </div>
        </div>
      ) : null}

      {/* Final root cause */}
      <Card
        className={
          investigation.final_root_cause
            ? "border-teal-200/80 bg-gradient-to-br from-white to-[var(--accent-muted)]"
            : undefined
        }
      >
        <p className="m-0 text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--accent)]">
          Final root cause
        </p>
        {investigation.final_root_cause ? (
          <p className="m-0 mt-3 text-base font-medium leading-relaxed sm:text-lg">
            {investigation.final_root_cause}
          </p>
        ) : (
          <p className="m-0 mt-3 text-sm leading-relaxed text-[var(--muted)]">
            {active
              ? "Still investigating — root cause will appear when the run finishes."
              : "No root cause asserted (needs human review)."}
          </p>
        )}
      </Card>

      {/* Hypotheses */}
      <Card>
        <div className="mb-4 flex flex-wrap items-end justify-between gap-2 border-b border-[var(--border)] pb-4">
          <div>
            <CardTitle>Hypotheses</CardTitle>
            <CardDescription>
              Ranked by confidence score (highest first).
            </CardDescription>
          </div>
          <span className="font-[family-name:var(--font-mono)] text-xs text-[var(--muted-soft)]">
            {hypotheses.length} ranked
          </span>
        </div>

        {hypotheses.length === 0 ? (
          <p className="m-0 text-sm text-[var(--muted)]">
            {active ? "Hypotheses will appear as the run progresses." : "No hypotheses."}
          </p>
        ) : (
          <ol className="m-0 list-none space-y-3 p-0">
            {hypotheses.map((hypothesis, index) => (
              <li
                key={`${hypothesis.description}-${index}`}
                className="rounded-[10px] border border-[var(--border)] bg-[var(--background-elevated)] px-4 py-3.5"
              >
                <div className="flex flex-wrap items-start gap-3">
                  <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-white font-[family-name:var(--font-mono)] text-xs font-semibold text-[var(--muted)] ring-1 ring-[var(--border)]">
                    {index + 1}
                  </span>
                  <div className="min-w-0 flex-1 space-y-3">
                    <p className="m-0 text-sm font-medium leading-relaxed">
                      {hypothesis.description}
                    </p>
                    <ConfidenceBar
                      value={hypothesis.confidence_score}
                      label="Confidence"
                    />
                    {hypothesis.supporting_evidence?.length ? (
                      <div className="flex flex-wrap gap-1.5">
                        {hypothesis.supporting_evidence.map((source) => (
                          <span
                            key={source}
                            className="rounded-md bg-white px-2 py-0.5 font-[family-name:var(--font-mono)] text-[11px] text-[var(--muted)] ring-1 ring-[var(--border)]"
                          >
                            {source}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>

      {/* Evidence */}
      <Card>
        <div className="mb-4 flex flex-wrap items-end justify-between gap-2 border-b border-[var(--border)] pb-4">
          <div>
            <CardTitle>Evidence</CardTitle>
            <CardDescription>
              {investigation.evidence.length === 0
                ? active
                  ? "Findings will appear as specialists report."
                  : "No evidence recorded."
                : investigation.evidence.length === 1
                  ? "1 finding gathered."
                  : `${investigation.evidence.length} findings, grouped by source.`}
            </CardDescription>
          </div>
        </div>

        {evidenceGroups.length === 0 ? (
          <p className="m-0 text-sm text-[var(--muted)]">No evidence yet.</p>
        ) : (
          <div className="space-y-5">
            {evidenceGroups.map(([source, items]) => (
              <section key={source}>
                <h3 className="m-0 mb-2.5 flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-[0.1em] text-[var(--muted)]">
                  <span className="rounded-md bg-[var(--accent-muted)] px-2 py-0.5 font-[family-name:var(--font-mono)] normal-case tracking-normal text-[var(--accent)]">
                    {source}
                  </span>
                  <span className="font-normal normal-case tracking-normal text-[var(--muted-soft)]">
                    {items.length} {items.length === 1 ? "finding" : "findings"}
                  </span>
                </h3>
                <ul className="m-0 list-none space-y-2.5 p-0">
                  {items.map((item, index) => (
                    <li
                      key={`${source}-${index}`}
                      className="rounded-[10px] border border-[var(--border)] bg-white px-4 py-3"
                    >
                      <div className="mb-2 flex items-center justify-between gap-3">
                        <span className="text-[11px] font-medium uppercase tracking-wide text-[var(--muted-soft)]">
                          Confidence
                        </span>
                        <span className="font-[family-name:var(--font-mono)] text-xs tabular-nums text-[var(--foreground)]">
                          {item.confidence.toFixed(2)}
                        </span>
                      </div>
                      <ConfidenceBar value={item.confidence} />
                      <p className="m-0 mt-3 text-sm leading-relaxed text-[var(--foreground)]">
                        {item.finding}
                      </p>
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
        )}
      </Card>

      <div className="flex flex-wrap gap-2 pt-1">
        <Link
          to="/history"
          className="inline-flex items-center justify-center rounded-[10px] border border-[var(--border)] bg-white px-4 py-2 text-sm font-semibold shadow-sm transition hover:border-[var(--border-strong)]"
        >
          History
        </Link>
        <Link
          to="/"
          className="inline-flex items-center justify-center rounded-[10px] border border-transparent px-4 py-2 text-sm font-semibold text-[var(--muted)] transition hover:bg-black/[0.03] hover:text-[var(--foreground)]"
        >
          New investigation
        </Link>
      </div>
    </div>
  );
}
