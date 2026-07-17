from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path):
    connection = kb.connect(tmp_path / "kanban.db")
    try:
        yield connection
    finally:
        connection.close()


def test_epic_progress_comes_only_from_explicit_membership(conn):
    epic = kb.create_task(conn, title="Portfolio outcome", work_item_kind="epic")
    member = kb.create_task(conn, title="Member")
    dependency = kb.create_task(conn, title="Acceptance dependency")
    kb.add_epic_membership(conn, epic_id=epic, task_id=member)
    kb.link_tasks(conn, dependency, member)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (member,))

    assert kb.epic_id_for_task(conn, member) == epic
    assert kb.epic_id_for_task(conn, dependency) is None
    assert kb.epic_progress(conn, epic) == {
        "done": 1,
        "total": 1,
        "release_state": "pending",
    }


def test_title_and_dependency_edges_never_create_epic_behavior(conn):
    titled_parent = kb.create_task(conn, title="Epic: only a title")
    child = kb.create_task(conn, title="Standalone card")
    kb.link_tasks(conn, titled_parent, child)

    assert kb._is_epic_task(conn, titled_parent) is False
    assert kb.epic_id_for_task(conn, child) is None
    assert kb.release_scope_for_task(conn, child) == "standalone"


def test_one_card_has_at_most_one_epic_but_dependencies_remain_unbounded(conn):
    first = kb.create_task(conn, title="First outcome", work_item_kind="epic")
    second = kb.create_task(conn, title="Second outcome", work_item_kind="epic")
    card = kb.create_task(conn, title="Card")
    dependency_a = kb.create_task(conn, title="Dependency A")
    dependency_b = kb.create_task(conn, title="Dependency B")
    kb.add_epic_membership(conn, epic_id=first, task_id=card)
    kb.link_tasks(conn, dependency_a, card)
    kb.link_tasks(conn, dependency_b, card)

    with pytest.raises(Exception):
        kb.add_epic_membership(conn, epic_id=second, task_id=card)
    assert kb.parent_ids(conn, card) == sorted([dependency_a, dependency_b])
    assert kb.epic_id_for_task(conn, card) == first


def test_epic_members_may_enter_at_different_valid_phases(conn):
    epic = kb.create_task(conn, title="Cross-phase outcome", work_item_kind="epic")
    architecture = kb.create_task(
        conn,
        title="Architecture member",
        assignee="architect",
        workflow_template_id="product",
        current_step_key="architecture",
    )
    review = kb.create_task(
        conn,
        title="Review member",
        assignee="reviewer",
        workflow_template_id="product",
        current_step_key="review",
    )
    kb.add_epic_membership(conn, epic_id=epic, task_id=architecture)
    kb.add_epic_membership(conn, epic_id=epic, task_id=review)

    assert kb.list_epic_members(conn, epic) == sorted([architecture, review])
    assert kb.get_task(conn, architecture).current_step_key == "architecture"
    assert kb.get_task(conn, review).current_step_key == "review"


def test_epic_cannot_be_completed_by_an_ordinary_task_completion(conn):
    epic = kb.create_task(conn, title="Release container", work_item_kind="epic")
    member = kb.create_task(conn, title="Done member")
    kb.add_epic_membership(conn, epic_id=epic, task_id=member)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (member,))

    assert kb.complete_task(conn, epic, summary="members done") is False
    assert kb.get_task(conn, epic).status != "done"
    assert any(
        event.kind == "completion_blocked_epic_release"
        for event in kb.list_events(conn, epic)
    )
