import type { TextareaHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export function Textarea({
  className,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={cn(
        "min-h-36 w-full resize-y rounded-md border border-[var(--border)] bg-white px-3 py-2 text-sm outline-none ring-[var(--ring)] focus:ring-2",
        className,
      )}
      {...props}
    />
  );
}
