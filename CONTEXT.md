# Hermes Development Workflow

This context defines how development work gains authority and moves through Hermes-managed product boards.

## Language

**Work Inbox**:
The authenticated Hermes boundary through which a Local External AI submits Ole-approved new work or returns a Hermes-assigned delivery. A submission grants admission to the framework, but no authority to change project state or execute work.
_Avoid_: Qualification Inbox, Qualified Work Inbox

**Local External AI**:
An AI running locally but outside the Hermes-managed framework, profiles, assignments, and workflow authority.
_Avoid_: Remote integration, Hermes worker

**Admission Approval**:
Ole's decision that new work may enter the Hermes framework. It exists before Work Inbox submission and is distinct from qualification and execution authority.
_Avoid_: Work Contract, qualification, implementation approval

**New Work Submission**:
A description of a desired outcome that has Admission Approval and no current Work Contract. Its payload remains validated and inert while the framework qualifies it or requests clarification.
_Avoid_: Unapproved proposal, card, qualified work

**Assigned Delivery**:
An external worker's result for the exact task, run, and Work Contract assigned by Hermes. Hermes validates it and applies it through Normal Handover.
_Avoid_: Direct completion, external routing

**Qualification**:
Hermes and the Product Owner structure a New Work Submission with Admission Approval into scoped, routed work with a signed Work Contract. Qualification does not decide whether the work may enter Hermes.
_Avoid_: Admission approval, recovery, requalification, client tag, direct board creation

**Requalification**:
Hermes issues a successor Work Contract for an existing card, then returns that card to the normal handover flow.
_Avoid_: Override, separate recovery lifecycle

**Break-glass Override**:
An authenticated, audited instruction from Ole directly to Hermes that may bypass ordinary qualification policy.
_Avoid_: Requalification, admin edit

**Work Contract**:
The immutable, signed execution authority defining a card's scope, routing, handover, and operating rules.
_Avoid_: Editable metadata, label

**Normal Handover**:
The Hermes-owned transition that validates a delivery and passes the same card and its evidence to the next qualified phase and role.
_Avoid_: Direct orchestration, manual status edit
