import type { UpdateCheckResponse } from "@/lib/api";

export function formatHermesSyncSummary(
  info: UpdateCheckResponse,
): string | null {
  const upstreamBehind = info.upstream_behind;
  if (info.update_available || info.behind !== 0) {
    return null;
  }
  if (info.sync_state === "NEEDS_OLE") {
    return "Installed current · Official upstream sync needs attention";
  }
  if (info.sync_state === "LOCKED") {
    return "Installed current · Official upstream sync already running";
  }
  if (upstreamBehind == null || upstreamBehind <= 0) return null;
  const noun = upstreamBehind === 1 ? "commit" : "commits";
  const active = new Set(["PR_UPDATED", "PENDING_REFRESH", "REFRESH_REQUIRED"]);
  const suffix = active.has(info.sync_state ?? "") ? "syncing" : "pending";
  return `Installed current · ${upstreamBehind} official upstream ${noun} ${suffix}`;
}
