# Kanban Governance Security Follow-up Design

## Goal

Close the four Task 6 governance gaps without broadening the model tool surface or changing live configuration.

## Design

Terminal governance uses a conservative classifier. Explicitly validated read-only commands remain allowed. Known mutators expose literal filesystem targets. Opaque commands, interpreter or shell wrappers, and commands whose mutation targets cannot be proven are ambiguous and therefore blocked for workers. Human operations remain allowed outside governed projects, while known targets inside governed projects require an exact approval.

V4A patch targets are parsed by one canonical helper in `tools.file_tools`, shared by the file tool and governance plugin. Every Update/Add/Delete/Move endpoint is resolved with the same task/session-relative semantics as execution. Humans require approval when any target is governed; workers may patch only when every target belongs to their card project and workspace.

Pre-tool directive precedence is `block` over governance one-shot approval over ordinary approval. The one-shot record and audit consumer remain paired, so another plugin cannot downgrade exact approval into reusable approval.

Plugin manifests gain a generic nested boolean config gate. `kanban-governance` declares `plugins.kanban-governance.enabled`, and generic `plugins.enabled` cannot activate a manifest with that exact gate unless the nested value is literally `true`.

## Error Handling

Workers fail closed on unresolved paths, opaque mutators, missing patch targets, mismatched project/workspace/branch, privileged Git/deploy operations, or missing one-shot consumers. Read-only operations retain their current permissive behavior.

## Testing

Regression tests cover every reproduced escape, positive read-only and in-workspace mutations, V4A human/worker boundaries, approval ordering, exact config values, and the existing one-shot audit path. All commands run through `scripts/run_tests.sh`; final verification includes focused suites, Ruff, and `git diff --check`.
