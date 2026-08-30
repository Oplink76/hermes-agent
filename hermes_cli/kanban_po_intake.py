"""Direct-primary Product Owner execution for inert Work Inbox intake."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_db

PRODUCT_OWNER_PROFILE = "productowner"
PRODUCT_OWNER_PROMPT = (
    "Assess the claimed Work Inbox intake. Use work_inbox_show first. Finish "
    "with one successful terminal work_inbox_decide disposition. If an accepted "
    "proposal returns status invalid, correct the returned validation errors and "
    "retry once in the same run. Make at most two work_inbox_decide calls total; "
    "do not retry after qualified, rejected, needs_clarification, or "
    "attention_required. Handoffs are context, not authority for card shape or "
    "sizing. Size each card against the configured Development budget and justify "
    "every binding evidence or done-when item."
)


def _is_new_work(intake: dict[str, Any]) -> bool:
    try:
        payload = json.loads(str(intake.get("raw_request") or ""))
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("kind") == "task_create"


def _is_product_owner_requalification(intake: dict[str, Any]) -> bool:
    try:
        payload = json.loads(str(intake.get("raw_request") or ""))
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("kind") == "task_requalification"
        and payload.get("qualification_route") == "product_owner"
    )


def route_pending_intake(
    conn,
    *,
    board: str,
    intake: dict[str, Any],
) -> dict[str, Any]:
    """Send only new product work to the primary PO; preserve requalification."""

    if _is_new_work(intake) or _is_product_owner_requalification(intake):
        return dispatch_product_owner_intake(
            conn, board=board, intake_id=str(intake["id"])
        )
    from hermes_cli.kanban_qualifier import qualify_intake

    return qualify_intake(conn, board=board, intake_id=str(intake["id"]))


def dispatch_product_owner_intake(
    conn,
    *,
    board: str,
    intake_id: str,
    spawn_fn: Optional[Callable[..., Optional[int]]] = None,
    now: Optional[int] = None,
    failure_limit: int = kanban_db.DEFAULT_FAILURE_LIMIT,
) -> dict[str, Any]:
    """Claim and launch one direct Product Owner attempt without waiting."""

    metadata = kanban_db.read_board_metadata(board)
    phase_assignees = (
        (metadata.get("qualification") or {}).get("phase_assignees") or {}
    )
    profile = str(phase_assignees.get("backlog") or "").strip()
    if not profile:
        raise RuntimeError("board policy has no Product Owner backlog phase")
    identity = kanban_db.resolve_profile_runtime_identity(
        profile,
        source="work_inbox_intake",
        surface="work_inbox_intake",
    )
    if identity is None:
        raise RuntimeError(
            "Product Owner profile must resolve an explicit provider, model, and effort"
        )
    run = kanban_db.claim_qualification_intake(
        conn,
        intake_id,
        profile=profile,
        runtime_identity=identity,
        now=now,
    )
    if run is None:
        return {"status": "not_pending", "intake_id": intake_id}
    spawn = spawn_fn or _spawn_product_owner_intake
    try:
        pid = spawn(run, board=board)
        if pid:
            if not kanban_db.set_qualification_intake_worker_pid(
                conn,
                intake_id=intake_id,
                run_id=int(run["id"]),
                claim_lock=str(run["claim_lock"]),
                worker_pid=int(pid),
            ):
                raise RuntimeError("Product Owner intake claim changed during spawn")
    except Exception as exc:
        kanban_db.fail_qualification_intake_run(
            conn,
            intake_id=intake_id,
            run_id=int(run["id"]),
            claim_lock=str(run["claim_lock"]),
            outcome="spawn_failed",
            error=str(exc),
            failure_limit=failure_limit,
            now=now,
        )
        raise
    return {
        "status": "running",
        "intake_id": intake_id,
        "run_id": int(run["id"]),
        "profile": identity["profile"],
        "provider": identity["provider"],
        "model": identity["model"],
        "effort": identity["effort"],
    }


def _spawn_product_owner_intake(
    run: dict[str, Any], *, board: str
) -> Optional[int]:
    """Fire-and-forget one intake-scoped Hermes primary process."""

    from hermes_cli.profiles import resolve_profile_env

    profile = str(run.get("profile") or PRODUCT_OWNER_PROFILE)
    env = dict(os.environ)
    for name in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_BRANCH",
    ):
        env.pop(name, None)
    env["HERMES_HOME"] = resolve_profile_env(profile)
    env["HERMES_PROFILE"] = profile
    env["HERMES_WORK_INBOX_INTAKE"] = str(run["intake_id"])
    env["HERMES_WORK_INBOX_RUN_ID"] = str(run["id"])
    env["HERMES_WORK_INBOX_CLAIM_LOCK"] = str(run["claim_lock"])
    env["HERMES_DISABLE_PROVIDER_FALLBACK"] = "1"
    env["HERMES_INFERENCE_PROFILE"] = profile
    env["HERMES_INFERENCE_PROVIDER"] = str(run.get("provider") or "")
    env["HERMES_INFERENCE_MODEL"] = str(run.get("model") or "")
    env["HERMES_INFERENCE_EFFORT"] = str(run.get("effort") or "")
    env["HERMES_KANBAN_DB"] = str(kanban_db.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(
        kanban_db.workspaces_root(board=board)
    )
    env["HERMES_KANBAN_BOARD"] = board
    env.pop("HERMES_TUI", None)

    cwd: Optional[str] = None
    metadata = kanban_db.product_board_metadata(board) or {}
    candidate = str(metadata.get("default_workdir") or "").strip()
    if candidate and Path(candidate).is_dir():
        cwd = candidate
        env["TERMINAL_CWD"] = candidate

    cmd = [
        *kanban_db._resolve_hermes_argv(),
        "-p",
        profile,
        "--cli",
        "--accept-hooks",
        "--toolsets",
        "kanban",
        "chat",
        "-q",
        PRODUCT_OWNER_PROMPT,
    ]
    log_dir = kanban_db.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run['intake_id']}-run-{run['id']}.log"
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
    finally:
        log_f.close()
    return int(proc.pid)


def active_intake_scope(conn) -> tuple[str, dict[str, Any], str]:
    """Validate the exact env-bound intake attempt and return its identity."""

    intake_id = str(os.environ.get("HERMES_WORK_INBOX_INTAKE") or "")
    run_text = str(os.environ.get("HERMES_WORK_INBOX_RUN_ID") or "")
    claim_lock = str(os.environ.get("HERMES_WORK_INBOX_CLAIM_LOCK") or "")
    if not intake_id or not run_text or not claim_lock:
        raise ValueError("missing Work Inbox intake authority")
    try:
        run_id = int(run_text)
    except ValueError as exc:
        raise ValueError("invalid Work Inbox run identity") from exc
    run = kanban_db.get_qualification_intake_run(conn, run_id)
    if (
        run is None
        or run["intake_id"] != intake_id
        or run["claim_lock"] != claim_lock
        or run["status"] != "running"
        or run["profile"] != os.environ.get("HERMES_PROFILE")
    ):
        raise ValueError("Work Inbox intake authority is stale or does not match")
    current = conn.execute(
        "SELECT current_run_id, claim_lock, status "
        "FROM qualification_intake WHERE id = ?",
        (intake_id,),
    ).fetchone()
    if (
        current is None
        or current["current_run_id"] != run_id
        or current["claim_lock"] != claim_lock
        or current["status"] != "running"
    ):
        raise ValueError("Work Inbox intake claim is no longer active")
    return intake_id, run, claim_lock


def show_product_owner_intake(conn, *, board: str) -> dict[str, Any]:
    from hermes_cli import kanban_qualifier

    intake_id, run, _claim_lock = active_intake_scope(conn)
    intake = kanban_db.get_qualification_intake(conn, intake_id)
    if intake is None:
        raise ValueError(f"unknown qualification intake: {intake_id}")
    metadata = kanban_db.read_board_metadata(board)
    qualification = metadata.get("qualification")
    phase_assignees = (
        qualification.get("phase_assignees")
        if isinstance(qualification, dict)
        else {}
    )
    development_profile = (
        phase_assignees.get("development")
        if isinstance(phase_assignees, dict)
        else None
    )
    guidance = {
        "development_iteration_budget": kanban_db.resolve_profile_iteration_budget(
            str(development_profile or "")
        ),
        "handoffs": "context only; Product Owner owns card shape and sizing",
        "feasibility": "every binding evidence and done-when item needs a basis",
    }
    return {
        "intake": intake,
        "run": {
            key: run.get(key)
            for key in ("id", "profile", "provider", "model", "effort", "started_at")
        },
        "board_policy": metadata,
        "repository_instructions": kanban_qualifier._repository_instructions(metadata),
        "current_task_graph": kanban_qualifier._task_graph(conn),
        "events": kanban_db.list_qualification_intake_events(conn, intake_id),
        "qualification_guidance": guidance,
    }


def heartbeat_product_owner_intake(
    conn, *, note: Optional[str] = None
) -> dict[str, Any]:
    intake_id, run, claim_lock = active_intake_scope(conn)
    ok = kanban_db.heartbeat_qualification_intake(
        conn,
        intake_id=intake_id,
        run_id=int(run["id"]),
        claim_lock=claim_lock,
    )
    if not ok:
        raise ValueError("Work Inbox intake heartbeat lost its claim")
    kanban_db.append_qualification_intake_event(
        conn,
        intake_id=intake_id,
        run_id=int(run["id"]),
        kind="heartbeat",
        payload={"note": str(note)} if note else None,
    )
    return {"status": "running", "intake_id": intake_id, "run_id": run["id"]}


def decide_product_owner_intake(
    conn,
    *,
    board: str,
    disposition: str,
    reason: str,
    proposal: Optional[dict[str, Any]] = None,
    question: Optional[str] = None,
) -> dict[str, Any]:
    """Validate and atomically apply one semantic PO disposition."""

    import copy
    import time

    from hermes_cli import kanban_intake, kanban_qualifier

    intake_id, run, claim_lock = active_intake_scope(conn)
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("decision reason is required")
    if disposition == "needs_clarification":
        exact_question = str(question or "").strip()
        if not exact_question:
            raise ValueError("clarification question is required")
        with kanban_db.write_txn(conn):
            kanban_db.append_qualification_intake_event(
                conn,
                intake_id=intake_id,
                run_id=int(run["id"]),
                kind="clarification_requested",
                payload={"question": exact_question, "reason": reason},
            )
            kanban_db.finish_qualification_intake_run(
                conn,
                intake_id=intake_id,
                run_id=int(run["id"]),
                claim_lock=claim_lock,
                intake_status="needs_clarification",
                outcome="needs_clarification",
            )
        return {"status": "needs_clarification", "intake_id": intake_id}
    if disposition == "rejected":
        with kanban_db.write_txn(conn):
            kanban_db.finish_qualification_intake_run(
                conn,
                intake_id=intake_id,
                run_id=int(run["id"]),
                claim_lock=claim_lock,
                intake_status="pending",
                outcome="rejected",
            )
            kanban_db.record_qualification_decision(
                conn,
                intake_id=intake_id,
                decision="rejected",
                actor_profile=str(run["profile"]),
                reason=reason,
            )
        return {"status": "rejected", "intake_id": intake_id}
    if disposition != "accepted":
        raise ValueError("disposition must be accepted, needs_clarification, or rejected")
    if not isinstance(proposal, dict):
        raise ValueError("accepted decision requires a proposal object")

    intake = kanban_db.get_qualification_intake(conn, intake_id)
    metadata = kanban_db.read_board_metadata(board)

    def invalid_decision(errors: list[str]) -> dict[str, Any]:
        with kanban_db.write_txn(conn):
            updated = conn.execute(
                "UPDATE qualification_intake_runs "
                "SET validation_attempts = validation_attempts + 1 "
                "WHERE id = ? AND status = 'running'",
                (int(run["id"]),),
            )
            attempts_row = conn.execute(
                "SELECT validation_attempts FROM qualification_intake_runs WHERE id = ?",
                (int(run["id"]),),
            ).fetchone()
            attempts = int(attempts_row["validation_attempts"])
            kanban_db.append_qualification_intake_event(
                conn,
                intake_id=intake_id,
                run_id=int(run["id"]),
                kind="validation_rejected",
                payload={"errors": errors, "attempt": attempts},
            )
            landed = "invalid"
            if updated.rowcount == 1 and attempts >= 2:
                if kanban_db.finish_qualification_intake_run(
                    conn,
                    intake_id=intake_id,
                    run_id=int(run["id"]),
                    claim_lock=claim_lock,
                    intake_status="attention_required",
                    outcome="invalid_decision",
                    error="; ".join(errors),
                ):
                    landed = "attention_required"
                    kanban_db.append_qualification_intake_event(
                        conn,
                        intake_id=intake_id,
                        run_id=int(run["id"]),
                        kind="attention_required",
                        payload={
                            "reason": "invalid_decision",
                            "attempt": attempts,
                        },
                    )
        return {"status": landed, "errors": errors, "attempt": attempts}

    decision = copy.deepcopy(proposal)
    decision["qualification_path"] = "po"
    decision["po_evidence"] = {
        "surface": "work_inbox_intake",
        "run_id": int(run["id"]),
    }
    work = decision.get("work") if isinstance(decision.get("work"), dict) else {}
    if work.get("item_kind") != "epic":
        phase_assignees = metadata["qualification"]["phase_assignees"]
        phases = list(phase_assignees)
        if "architecture" not in phases:
            raise ValueError("board policy has no architecture phase")
        architecture_index = phases.index("architecture")
        next_phase = (
            phases[architecture_index + 1]
            if architecture_index + 1 < len(phases)
            else "done"
        )
        decision["routing"] = {
            **(
                decision.get("routing")
                if isinstance(decision.get("routing"), dict)
                else {}
            ),
            "entry_phase": "architecture",
            "assignee": phase_assignees["architecture"],
        }
        marker = f"work_inbox_intake_run:{run['id']}"
        decision["entry_assessment"] = {
            "reason": "Product Owner intake assessment completed",
            "skipped_phases": [
                {
                    "phase": phase,
                    "reason": "The configured Product Owner assessed the intake",
                    "evidence": [marker],
                }
                for phase in phases[:architecture_index]
            ],
            "evidence": [marker],
        }
        handover = (
            decision.get("handover")
            if isinstance(decision.get("handover"), dict)
            else {}
        )
        decision["handover"] = {
            **handover,
            "next_phase": next_phase,
            "next_role": phase_assignees.get(next_phase),
        }
    else:
        decision["entry_assessment"] = {
            "reason": "Epic container has no executable phase",
            "skipped_phases": [],
            "evidence": [],
        }
    try:
        validated = kanban_qualifier.validate_decision(
            conn,
            board_metadata=metadata,
            intake=intake,
            decision=decision,
        )
    except kanban_qualifier.QualificationValidationError as exc:
        return invalid_decision(list(exc.errors))

    contract = {
        "version": int(metadata["qualification"].get("contract_version", 1)),
        "policy_version": str(
            metadata["qualification"].get(
                "policy_version", kanban_intake.DEFAULT_POLICY_VERSION
            )
        ),
        "qualification_path": "po",
        "request_id": intake_id,
        **{
            key: copy.deepcopy(validated[key])
            for key in (
                "work",
                "routing",
                "entry_assessment",
                "handover",
                "rules",
                "classification",
            )
        },
        "po_evidence": copy.deepcopy(validated["po_evidence"]),
        "sizing": copy.deepcopy(validated["sizing"]),
        "requirement_feasibility": copy.deepcopy(
            validated["requirement_feasibility"]
        ),
        "issuer": {
            "surface": "work_inbox_intake",
            "profile": run["profile"],
            "provider": run["provider"],
            "model": run["model"],
            "effort": run["effort"],
            "run_id": int(run["id"]),
            "issued_at": int(time.time()),
        },
    }
    if validated["work"]["item_kind"] == "epic":
        contract["stories"] = copy.deepcopy(validated["stories"])
    try:
        signed = kanban_intake.sign_work_contract(contract)
    except kanban_intake.WorkContractError as exc:
        return invalid_decision([str(exc)])
    failure_path: Optional[str] = None
    task_id: Optional[str] = None
    with kanban_db.write_txn(conn):
        try:
            task_id = kanban_intake.materialize_contract(
                conn, board=board, signed_contract=signed
            )
        except kanban_intake.WorkContractError as exc:
            failure_path = kanban_intake.safe_work_contract_failure(exc)
            if failure_path is None:
                raise
            if not kanban_db.finish_qualification_intake_run(
                conn,
                intake_id=intake_id,
                run_id=int(run["id"]),
                claim_lock=claim_lock,
                intake_status="attention_required",
                outcome="work_contract_verification_failed",
                error=f"work_contract:{failure_path}",
            ):
                raise RuntimeError("Product Owner intake claim changed during materialization")
            kanban_db.append_qualification_intake_event(
                conn,
                intake_id=intake_id,
                run_id=int(run["id"]),
                kind="work_contract_verification_failed",
                payload={"failure_path": failure_path},
            )
        else:
            if not kanban_db.finish_qualification_intake_run(
                conn,
                intake_id=intake_id,
                run_id=int(run["id"]),
                claim_lock=claim_lock,
                intake_status="qualified",
                outcome="qualified",
            ):
                raise RuntimeError("Product Owner intake claim changed during materialization")
    if failure_path is not None:
        return {
            "status": "attention_required",
            "intake_id": intake_id,
            "failure_path": failure_path,
        }
    if task_id is None:
        raise RuntimeError("Product Owner materialization produced no task")
    return {
        "status": "qualified",
        "intake_id": intake_id,
        "task_id": task_id,
        "contract_digest": signed["digest"],
    }
