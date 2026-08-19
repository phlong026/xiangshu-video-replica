import { AdminApp } from "./AdminApp";
import { App } from "./App";

export function RootApp({
  path = window.location.pathname,
}: {
  path?: string;
}) {
  return path === "/admin" || path.startsWith("/admin/") ? (
    <AdminApp />
  ) : (
    <App />
  );
}
