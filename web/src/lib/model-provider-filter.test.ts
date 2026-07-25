import { describe, expect, it } from "vitest";

import { filterModelOptionProviders } from "./model-provider-filter";

const providers = [
  { name: "Claude", slug: "claude-cli", models: ["default"] },
  { name: "Codex", slug: "codex-cli", models: ["default"] },
  { name: "Cowork", slug: "cowork", models: ["default"] },
];

describe("model picker provider filtering", () => {
  it("keeps all local agents in a normal primary picker", () => {
    expect(filterModelOptionProviders(providers).map((p) => p.slug)).toEqual([
      "claude-cli",
      "codex-cli",
      "cowork",
    ]);
  });

  it("excludes Cowork only when the MoA picker asks for it", () => {
    expect(
      filterModelOptionProviders(providers, ["moa", "cowork"]).map(
        (p) => p.slug,
      ),
    ).toEqual(["claude-cli", "codex-cli"]);
  });
});
