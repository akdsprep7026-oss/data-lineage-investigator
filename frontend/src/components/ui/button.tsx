import type { ButtonHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "secondary";
  size?: "default" | "sm";
};

export function Button({
  className,
  variant = "primary",
  size = "default",
  type = "button",
  ...props
}: Props) {
  return (
    <button
      type={type}
      className={cn(
        "inline-flex items-center justify-center gap-2 font-semibold tracking-tight transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        size === "default" && "rounded-[10px] px-4 py-2.5 text-sm",
        size === "sm" && "rounded-lg px-3 py-1.5 text-xs",
        variant === "primary" &&
          "bg-[var(--accent)] text-[var(--accent-foreground)] shadow-sm hover:bg-[var(--accent-hover)]",
        variant === "secondary" &&
          "border border-[var(--border)] bg-white text-[var(--foreground)] shadow-sm hover:border-[var(--border-strong)] hover:bg-[var(--background-elevated)]",
        variant === "ghost" &&
          "border border-transparent bg-transparent text-[var(--muted)] hover:bg-black/[0.03] hover:text-[var(--foreground)]",
        className,
      )}
      {...props}
    />
  );
}
