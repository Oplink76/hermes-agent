import type { UpdateCheckResponse } from "@/lib/api";

export function formatHermesSyncSummary(
  info: UpdateCheckResponse,
): string | null {
  const upstreamBehind = info.upstream_behind;
  if (
    info.update_available ||
    info.behind !== 0 ||
    upstreamBehind == null ||
    upstreamBehind <= 0
  ) {
    return null;
  }
  const noun = upstreamBehind === 1 ? "commit" : "commits";
  return `Installed current · ${upstreamBehind} official upstream ${noun} syncing`;
}
