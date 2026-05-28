import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trading v2",
  description: "ICT Multi-TF Bot + Backtest",
};

const navItems = [
  { href: "/", label: "Dashboard" },
  { href: "/backtest", label: "Backtest" },
  { href: "/live", label: "Live" },
  { href: "/tuner", label: "Tuner" },
  { href: "/chat", label: "Chat" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="de">
      <body>
        <header
          style={{
            background: "var(--surface)",
            borderBottom: "1px solid var(--border)",
            padding: "0 24px",
            height: 52,
            display: "flex",
            alignItems: "center",
            gap: 32,
            position: "sticky",
            top: 0,
            zIndex: 50,
          }}
        >
          <span style={{ fontWeight: 700, fontSize: 15, color: "var(--accent)" }}>
            trading-v2
          </span>
          <nav style={{ display: "flex", gap: 24 }}>
            {navItems.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                style={{
                  color: "var(--text-dim)",
                  fontSize: 14,
                  textDecoration: "none",
                  fontWeight: 500,
                }}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        </header>
        <main style={{ padding: "24px", maxWidth: 1400, margin: "0 auto" }}>
          {children}
        </main>
      </body>
    </html>
  );
}
