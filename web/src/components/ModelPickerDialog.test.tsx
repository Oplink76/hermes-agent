// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ModelPickerDialog } from "./ModelPickerDialog";

const localProviders = [
  {
    authenticated: true,
    models: ["default"],
    name: "Claude CLI (local agent)",
    slug: "claude-cli",
  },
  {
    authenticated: true,
    models: ["default"],
    name: "Codex CLI (local agent)",
    slug: "codex-cli",
  },
  {
    authenticated: true,
    models: ["default"],
    name: "Cowork (local agent)",
    slug: "cowork",
  },
];

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

async function renderPicker(
  providers = localProviders,
  onApply = vi.fn().mockResolvedValue(undefined),
  excludeProviders: string[] = [],
) {
  await act(async () => {
    root.render(
      <ModelPickerDialog
        alwaysGlobal
        excludeProviders={excludeProviders}
        loader={async () => ({ providers })}
        onApply={onApply}
        onClose={vi.fn()}
      />,
    );
    await Promise.resolve();
  });

  return onApply;
}

function elementWithExactText(text: string): HTMLElement {
  const match = Array.from(document.body.querySelectorAll<HTMLElement>("*"))
    .filter((element) => element.children.length === 0)
    .find((element) => element.textContent === text);
  if (!match) {
    throw new Error(`Missing element with text: ${text}`);
  }
  return match;
}

describe("ModelPickerDialog local-agent behavior", () => {
  it("renders all three normal primary providers and prerequisite status", async () => {
    await renderPicker([
      ...localProviders.slice(0, 2),
      {
        ...localProviders[2],
        authenticated: false,
        warning:
          "Unavailable: requires configured Cowork MCP cowork_run tool; no API key is used.",
      },
    ]);

    expect(document.body.textContent).toContain("Claude CLI (local agent)");
    expect(document.body.textContent).toContain("Codex CLI (local agent)");
    expect(document.body.textContent).toContain("Cowork (local agent)");

    await act(async () => {
      elementWithExactText("Cowork (local agent)").dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    expect(document.body.textContent).toContain(
      "requires configured Cowork MCP",
    );
  });

  it.each(localProviders)(
    "applies $slug from the real picker interaction",
    async (provider) => {
      const onApply = await renderPicker(
        localProviders.map((row) => ({
          ...row,
          is_current: row.slug === provider.slug,
        })),
      );

      await act(async () => {
        elementWithExactText(provider.name).dispatchEvent(
          new MouseEvent("click", { bubbles: true }),
        );
      });
      await act(async () => {
        elementWithExactText("default").dispatchEvent(
          new MouseEvent("dblclick", { bubbles: true }),
        );
        await Promise.resolve();
      });

      expect(onApply).toHaveBeenCalledWith({
        confirmExpensiveModel: false,
        model: "default",
        persistGlobal: true,
        provider: provider.slug,
      });
    },
  );

  it("keeps Claude/Codex but omits Cowork from an MoA-scoped picker", async () => {
    await renderPicker(localProviders, undefined, ["moa", "cowork"]);

    expect(document.body.textContent).toContain("Claude CLI (local agent)");
    expect(document.body.textContent).toContain("Codex CLI (local agent)");
    expect(document.body.textContent).not.toContain("Cowork (local agent)");
  });
});
