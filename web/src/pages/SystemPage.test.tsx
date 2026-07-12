import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { formatHermesSyncSummary } from "./system-update-status";

describe("formatHermesSyncSummary", () => {
  it("renders installed current separately from an official upstream backlog", () => {
    const text = formatHermesSyncSummary({
      install_method: "git",
      current_version: "0.17.0",
      behind: 0,
      fork_behind: 0,
      update_available: false,
      can_apply: true,
      update_command: "hermes update",
      message: null,
      upstream_behind: 54,
      sync_state: "PR_UPDATED",
      sync_pr_number: 7,
      sync_required_check: "All required checks pass",
      installed_sha: "a".repeat(40),
    });

    const html = renderToStaticMarkup(<span>{text}</span>);

    expect(html).toContain(
      "Installed current · 54 official upstream commits syncing",
    );
    expect(html).not.toContain("latest version");
  });

  it("renders an Ole escalation as attention needed instead of syncing", () => {
    const text = formatHermesSyncSummary({
      install_method: "git",
      current_version: "0.17.0",
      behind: 0,
      fork_behind: 0,
      update_available: false,
      can_apply: true,
      update_command: "hermes update",
      message: null,
      upstream_behind: 54,
      sync_state: "NEEDS_OLE",
      sync_pr_number: 7,
      sync_required_check: "All required checks pass",
      installed_sha: "a".repeat(40),
    });

    const html = renderToStaticMarkup(<span>{text}</span>);

    expect(html).toContain("Installed current · Official upstream sync needs attention");
    expect(html).not.toContain("syncing");
  });

  it("does not invent active syncing for an unknown state", () => {
    const text = formatHermesSyncSummary({
      install_method: "git",
      current_version: "0.17.0",
      behind: 0,
      update_available: false,
      can_apply: true,
      update_command: "hermes update",
      message: null,
      upstream_behind: 2,
      sync_state: null,
    });

    expect(text).toBe("Installed current · 2 official upstream commits pending");
  });
});
