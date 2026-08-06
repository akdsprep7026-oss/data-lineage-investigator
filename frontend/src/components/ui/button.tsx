import type { ButtonHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost";
};

export function Button({
  className,
  variant = "primary",
  type = "button",
  ...props
}: Props) {
  return (
    <button
      type={type}
      className={cn(
        "inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
        variant === "primary" &&
          "bg-[var(--accent)] text-[var(--accent-foreground)] hover:brightness-110",
        variant === "ghost" &&
          "border border-[var(--border)] bg-transparent text-[var(--foreground)] hover:bg-white/70",
        className,
      )}
      {...props}
    />
  );
}
