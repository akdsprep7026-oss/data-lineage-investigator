import { NavLink, Outlet } from "react-router-dom";
import { cn } from "../lib/utils";

const links = [
  { to: "/", label: "Submit" },
  { to: "/history", label: "History" },
];

export function Layout() {
  return (
    <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col px-4 py-6 sm:px-6 lg:px-8">
      <header className="mb-8 animate-fade-in sm:mb-10">
        <div className="flex flex-wrap items-end justify-between gap-4 border-b border-[var(--border)] pb-5">
          <div className="min-w-0">
            <p className="m-0 font-[family-name:var(--font-mono)] text-[11px] font-medium uppercase tracking-[0.16em] text-[var(--accent)]">
              Data Lineage Investigator
            </p>
            <h1 className="m-0 mt-1.5 text-2xl font-semibold tracking-tight text-[var(--foreground)] sm:text-[1.75rem]">
              Investigations
            </h1>
            <p className="m-0 mt-1 max-w-md text-sm text-[var(--muted)]">
              Multi-agent root-cause analysis for data pipeline incidents.
            </p>
          </div>
          <nav className="flex rounded-[10px] border border-[var(--border)] bg-white/80 p-1 shadow-sm backdrop-blur-sm">
            {links.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                end={link.to === "/"}
                className={({ isActive }) =>
                  cn(
                    "rounded-lg px-3.5 py-1.5 text-sm font-medium transition",
                    isActive
                      ? "bg-[var(--foreground)] text-white shadow-sm"
                      : "text-[var(--muted)] hover:text-[var(--foreground)]",
                  )
                }
              >
                {link.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="flex-1 animate-fade-in pb-12 [animation-delay:40ms]">
        <Outlet />
      </main>
    </div>
  );
}
