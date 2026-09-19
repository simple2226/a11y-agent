import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "a11y-agent",
  description:
    "Audits a public web page against WCAG 2.1 AA, generates fixes, and re-audits its own work.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
