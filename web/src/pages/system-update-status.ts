import type { UpdateCheckResponse } from "@/lib/api";

export function formatHermesSyncSummary(
  info: UpdateCheckResponse,
): string | null {
  const upstreamBehind = info.upstream_behind;
  if (info.sync_state === "NEEDS_OLE") {
    return "Installed current · Official upstream sync needs attention";
  }
  if (info.sync_state === "LOCKED") {
    return "Installed current · Official upstream sync already running";
  }
  if (info.update_available || info.behind !== 0) {
    return null;
  }
  if (upstreamBehind == null || upstreamBehind <= 0) return null;
  const noun = upstreamBehind === 1 ? "commit" : "commits";
  const active = new Set(["PR_UPDATED", "PENDING_REFRESH", "REFRESH_REQUIRED"]);
  const suffix = active.has(info.sync_state ?? "") ? "syncing" : "pending";
  if (info.sync_state === "ROLLED_BACK_REVERTED") {
    return `Installed current · ${upstreamBehind} official upstream ${noun} pending after safe rollback`;
  }
  return `Installed current · ${upstreamBehind} official upstream ${noun} ${suffix}`;
}

export function isHermesSyncUpdateBlocked(info: UpdateCheckResponse): boolean {
  return (
    info.sync_update_blocked === true ||
    info.sync_deployment_state === "merge_intent" ||
    info.sync_deployment_state === "merged_pending_deploy" ||
    info.sync_deployment_state === "crossed_invalid" ||
    info.sync_state === "NEEDS_OLE" ||
    info.sync_state === "ROLLED_BACK_REVERTED"
  );
}

export function canApplyHermesUpdate(info: UpdateCheckResponse): boolean {
  return info.update_available && info.can_apply && !isHermesSyncUpdateBlocked(info);
}
