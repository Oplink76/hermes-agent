from __future__ import annotations

import json
import stat
import subprocess
import sys

import pytest

from hermes_cli import backup
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_intake as intake


def _contract(**overrides):
    contract = {
        "version": 1,
        "policy_version": "product-handoff-v2+qualification-v1",
        "qualification_path": "hermes",
        "request_id": "qi_example",
        "work": {
            "item_kind": "card",
            "work_type": "story",
            "title": "Make qualified work visible",
            "outcome": "Only governed work reaches development",
            "scope": ["Hermes"],
            "out_of_scope": ["Cockpit"],
        },
        "routing": {
            "entry_phase": "development",
            "assignee": "developer",
            "epic_id": None,
            "dependencies": ["t_parent"],
        },
        "handover": {
            "deliverables": ["implementation"],
            "required_evidence": ["tests"],
            "done_when": ["green"],
            "next_phase": "test",
            "next_role": "tester",
        },
        "rules": {"allowed": ["repo edits"], "forbidden": ["break glass"]},
        "classification": ["framework:story", "path:hermes"],
        "issuer": {"profile": "hermes", "run_id": 42, "issued_at": 1_784_270_000},
    }
    contract.update(overrides)
    return contract


def test_canonical_contract_has_stable_json_and_digest():
    first = _contract()
    second = json.loads(json.dumps(first, sort_keys=True))

    assert intake.canonical_contract_json(first) == intake.canonical_contract_json(second)
    assert intake.contract_digest(first) == intake.contract_digest(second)


def test_service_signature_verifies_and_caller_signature_is_ignored():
    secret = b"test-only-secret"
    payload = _contract(signature="caller-controlled", digest="caller-controlled")

    signed = intake.sign_work_contract(payload, secret=secret)

    assert signed["signature"] != "caller-controlled"
    assert signed["digest"] != "caller-controlled"
    assert "signature" not in signed["contract"]
    assert intake.verify_work_contract(signed, secret=secret)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("routing", "entry_phase", "review"),
        ("routing", "assignee", "reviewer"),
        ("routing", "epic_id", "e_other"),
        ("routing", "dependencies", ["t_other"]),
        ("rules", "forbidden", ["different"]),
        ("issuer", "profile", "productowner"),
    ],
)
def test_mutating_governed_contract_fields_breaks_verification(section, field, value):
    signed = intake.sign_work_contract(_contract(), secret=b"test-only-secret")
    signed["contract"][section][field] = value

    assert not intake.verify_work_contract(signed, secret=b"test-only-secret")


@pytest.mark.parametrize("version", [None, 0, 2, "1"])
def test_missing_or_unknown_contract_versions_fail_closed(version):
    contract = _contract(version=version)

    with pytest.raises(intake.WorkContractError, match="version"):
        intake.sign_work_contract(contract, secret=b"test-only-secret")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits do not apply on NTFS")
def test_signing_secret_is_service_owned_private_and_in_quick_backup_manifest(tmp_path):
    signed = intake.sign_work_contract(_contract(), hermes_home=tmp_path)
    secret_path = tmp_path / intake.SIGNING_KEY_RELATIVE_PATH

    assert intake.verify_work_contract(signed, hermes_home=tmp_path)
    assert secret_path.is_file()
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert intake.SIGNING_KEY_RELATIVE_PATH in backup._QUICK_STATE_FILES
    assert secret_path.name in backup._SECRET_FILE_NAMES


def test_signing_secret_uses_owner_only_acl_on_windows(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(intake.sys, "platform", "win32")
    monkeypatch.setattr(intake.shutil, "which", lambda name: "C:/Windows/System32/icacls.exe")
    monkeypatch.setattr(intake.getpass, "getuser", lambda: "Hermes User")

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "processed", "")

    monkeypatch.setattr(intake.subprocess, "run", _run)

    path = tmp_path / intake.SIGNING_KEY_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 32)
    intake._restrict_signing_key_permissions(path)

    assert calls[0][0] == [
        "C:/Windows/System32/icacls.exe",
        str(path),
        "/inheritance:r",
        "/grant:r",
        "Hermes User:F",
    ]


def test_strict_board_requires_valid_contract_and_enforces_phase_role_mapping():
    secret = b"test-only-secret"
    board = {
        "preset": "product",
        "qualification": {
            "required": True,
            "contract_version": 1,
            "phase_assignees": {"development": "developer", "review": "reviewer"},
        },
    }

    with pytest.raises(intake.WorkContractError, match="required"):
        intake.materialization_fields(board, signed_contract=None, secret=secret)

    signed = intake.sign_work_contract(_contract(), secret=secret)
    fields = intake.materialization_fields(
        board,
        signed_contract=signed,
        secret=secret,
        caller_fields={"classification": ["caller:unsafe"], "assignee": "reviewer"},
    )
    assert fields["current_step_key"] == "development"
    assert fields["assignee"] == "developer"
    assert fields["classification"] == ["framework:story", "path:hermes"]

    signed["contract"]["routing"]["assignee"] = "reviewer"
    signed = intake.sign_work_contract(signed["contract"], secret=secret)
    with pytest.raises(intake.WorkContractError, match="phase.*assignee"):
        intake.materialization_fields(board, signed_contract=signed, secret=secret)


def test_strict_board_fails_closed_without_a_phase_role_mapping():
    signed = intake.sign_work_contract(_contract(), secret=b"test-only-secret")

    with pytest.raises(intake.WorkContractError, match="phase_assignees"):
        intake.materialization_fields(
            {"preset": "product", "qualification": {"required": True}},
            signed_contract=signed,
            secret=b"test-only-secret",
        )


def test_generic_board_preserves_caller_fields_without_contract():
    fields = intake.materialization_fields(
        {"preset": "generic"},
        signed_contract=None,
        caller_fields={"title": "Standalone work", "assignee": "default"},
    )

    assert fields == {"title": "Standalone work", "assignee": "default"}


def test_product_board_defaults_declare_policy_without_activating_it():
    defaults = kb.product_workflow_defaults_for_board("product")

    assert defaults["qualification"] == kb.PRODUCT_QUALIFICATION_DEFAULTS
    assert defaults["qualification"]["required"] is False
