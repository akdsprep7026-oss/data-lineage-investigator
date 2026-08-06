import { NavLink, Outlet } from "react-router-dom";
import { cn } from "../lib/utils";

const links = [
  { to: "/", label: "Submit" },
  { to: "/history", label: "History" },
];

export function Layout() {
  return (
    <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col px-4 py-6 sm:px-6">
      <header className="mb-8 flex flex-wrap items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
        <div>
          <p className="m-0 text-xs font-semibold uppercase tracking-[0.14em] text-[var(--muted)]">
            Data Lineage Investigator
          </p>
          <h1 className="m-0 mt-1 text-2xl font-semibold tracking-tight">
            Investigations
          </h1>
        </div>
        <nav className="flex gap-2">
          {links.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.to === "/"}
              className={({ isActive }) =>
                cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium text-[var(--muted)] transition hover:text-[var(--foreground)]",
                  isActive &&
                    "bg-white text-[var(--foreground)] shadow-sm ring-1 ring-[var(--border)]",
                )
              }
            >
              {link.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="flex-1 pb-10">
        <Outlet />
      </main>
    </div>
  );
}
