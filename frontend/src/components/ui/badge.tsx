import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";
import type { InvestigationStatus } from "../../lib/api";

const STATUS_STYLES: Record<InvestigationStatus, string> = {
  pending: "bg-slate-100 text-slate-700 ring-slate-200/80",
  investigating: "bg-[var(--info-muted)] text-[var(--info)] ring-sky-200/70",
  needs_human_review: "bg-[var(--warning-muted)] text-[var(--warning)] ring-amber-200/70",
  resolved: "bg-[var(--success-muted)] text-[var(--success)] ring-emerald-200/70",
};

const STATUS_DOT: Record<InvestigationStatus, string> = {
  pending: "bg-slate-400",
  investigating: "bg-[var(--info)] animate-pulse-soft",
  needs_human_review: "bg-[var(--warning)]",
  resolved: "bg-[var(--success)]",
};

export function Badge({
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-semibold tracking-tight ring-1 ring-inset",
        className,
      )}
      {...props}
    />
  );
}

export function StatusBadge({ status }: { status: InvestigationStatus }) {
  return (
    <Badge className={STATUS_STYLES[status]}>
      <span
        className={cn("size-1.5 shrink-0 rounded-full", STATUS_DOT[status])}
        aria-hidden
      />
      {status.replaceAll("_", " ")}
    </Badge>
  );
}
