import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";
import type { InvestigationStatus } from "../../lib/api";

const STATUS_STYLES: Record<InvestigationStatus, string> = {
  pending: "bg-slate-100 text-slate-700",
  investigating: "bg-sky-100 text-sky-800",
  needs_human_review: "bg-amber-100 text-amber-900",
  resolved: "bg-emerald-100 text-emerald-800",
};

export function Badge({
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
        className,
      )}
      {...props}
    />
  );
}

export function StatusBadge({ status }: { status: InvestigationStatus }) {
  return (
    <Badge className={STATUS_STYLES[status]}>
      {status.replaceAll("_", " ")}
    </Badge>
  );
}
