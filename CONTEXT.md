# Hermes Development Workflow

This context defines how development work gains authority and moves through Hermes-managed product boards.

## Language

**Work Inbox**:
The authenticated Hermes boundary through which a Local External AI submits Ole-approved new work or returns a Hermes-assigned delivery. A submission attests that Ole has approved admission to the framework, but grants no authority to change project state or execute work.
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

**Framework Maintenance Task**:
A bounded Hermes-internal operation initiated directly by Ole and tracked on the non-strict Default board. It uses lightweight Hermes ownership and evidence rather than Product Owner qualification or a product workflow.
_Avoid_: Project work, Work Inbox submission, untracked admin edit

**Governed Reconciliation**:
An exact, manifest-bound correction of legacy Hermes state authorized by Ole and applied through a narrow audited Hermes operation.
_Avoid_: Qualification, Requalification, normal delivery, general admin edit

**Break-glass Override**:
An authenticated, audited instruction from Ole directly to Hermes that may authorize one Governed Reconciliation or another explicit policy bypass.
_Avoid_: Requalification, admin edit

**Work Contract**:
The immutable, signed execution authority defining a qualified project card's scope, routing, handover, and operating rules. A Framework Maintenance Task instead uses Ole's direct authorization recorded on the Default board.
_Avoid_: Editable metadata, label

**Normal Handover**:
The Hermes-owned transition that validates a delivery and passes the same card and its evidence to the next qualified phase and role.
_Avoid_: Direct orchestration, manual status edit
