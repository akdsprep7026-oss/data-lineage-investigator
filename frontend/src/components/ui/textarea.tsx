import type { TextareaHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export function Textarea({
  className,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={cn(
        "min-h-40 w-full resize-y rounded-[10px] border border-[var(--border)] bg-[var(--background-elevated)] px-3.5 py-3 text-sm leading-relaxed text-[var(--foreground)] outline-none transition placeholder:text-[var(--muted-soft)] focus:border-[var(--accent)] focus:bg-white focus:ring-2 focus:ring-[var(--accent-muted)] disabled:cursor-not-allowed disabled:opacity-60",
        className,
      )}
      {...props}
    />
  );
}
