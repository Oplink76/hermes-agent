# Hermes Development Workflow

This context defines how development work gains authority and moves through Hermes-managed product boards.

## Language

**Work Inbox**:
The authenticated Hermes boundary through which an external AI submits new work or returns a Hermes-assigned delivery. A submission grants no authority to change project state.
_Avoid_: Qualification Inbox, Qualified Work Inbox

**New Work Submission**:
An untrusted description of a desired outcome that has no current Work Contract. It remains inert until initial qualification accepts or rejects it.
_Avoid_: Card, qualified work

**Assigned Delivery**:
An external worker's result for the exact task, run, and Work Contract assigned by Hermes. Hermes validates it and applies it through Normal Handover.
_Avoid_: Direct completion, external routing

**Qualification**:
Hermes or the Product Owner initially turns a New Work Submission into authorized work by issuing a signed Work Contract.
_Avoid_: Recovery, requalification, client tag, direct board creation

**Requalification**:
Hermes issues a successor Work Contract for an existing card, then returns that card to the normal handover flow.
_Avoid_: Override, separate recovery lifecycle

**Break-glass Override**:
An authenticated, audited instruction from Ole directly to Hermes that may bypass ordinary qualification policy.
_Avoid_: Requalification, admin edit

**Work Contract**:
The immutable, signed authority defining a card's scope, routing, handover, and operating rules.
_Avoid_: Editable metadata, label

**Normal Handover**:
The Hermes-owned transition that validates a delivery and passes the same card and its evidence to the next qualified phase and role.
_Avoid_: Direct orchestration, manual status edit
