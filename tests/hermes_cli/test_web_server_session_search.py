import asyncio
import time

from hermes_cli import web_server


class _FakeSessionDB:
    """Fake backing the /api/sessions/search endpoint.

    The endpoint surfaces direct session-id matches first, then FTS message
    matches, deduping both by compression lineage root. This fake has no
    compression chains (get_session returns no parent), so each session is its
    own lineage root.
    """

    closed = False

    def search_sessions_by_id(self, query, limit=20, include_archived=True):
        assert query == "20260603"
        assert include_archived is True
        return [
            {
                "id": "20260603_090200_exact",
                "preview": "ID match preview",
                "source": "cli",
                "model": "claude",
                "started_at": 100,
            }
        ]

    def search_messages(self, query, limit=20):
        assert query == "20260603*"
        return [
            {
                "session_id": "20260603_090200_exact",
                "snippet": "duplicate content hit should not replace ID hit",
                "role": "user",
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "session_id": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "session_started": 200,
            },
        ]

    def get_session(self, session_id):
        # No compression chains in this fixture — every session is its own root.
        return {"id": session_id, "parent_session_id": None}

    def get_compression_tip(self, session_id):
        return session_id

    def close(self):
        self.closed = True


def test_desktop_session_search_merges_id_matches_before_content_matches(monkeypatch):
    monkeypatch.setattr("hermes_state.SessionDB", _FakeSessionDB)

    response = asyncio.run(web_server.search_sessions(q="20260603", limit=2))

    # ID match surfaces first; the content hit on the SAME session is deduped
    # by lineage root (not double-listed); the unrelated content hit follows.
    assert response == {
        "results": [
            {
                "session_id": "20260603_090200_exact",
                "lineage_root": "20260603_090200_exact",
                "snippet": "ID match preview",
                "role": None,
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "session_id": "content_session",
                "lineage_root": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "session_started": 200,
            },
        ]
    }


class _BlockingListSessionDB:
    def __init__(self, *args, **kwargs):
        pass

    def list_sessions_rich(self, **kwargs):
        time.sleep(0.08)
        return []

    def session_count(self, **kwargs):
        return 0

    def close(self):
        pass


class _BlockingSearchSessionDB(_FakeSessionDB):
    def search_sessions_by_id(self, query, limit=20, include_archived=True):
        time.sleep(0.08)
        return super().search_sessions_by_id(query, limit, include_archived)


class _BlockingHydrationSessionDB:
    def __init__(self, *args, **kwargs):
        pass

    def resolve_session_id(self, session_id):
        return session_id

    def resolve_resume_session_id(self, session_id):
        return session_id

    def get_messages(self, session_id):
        time.sleep(0.08)
        return [{"role": "user", "content": "hello"}]

    def close(self):
        pass


async def _assert_loop_stays_responsive(awaitable):
    ticked = asyncio.Event()

    async def ticker():
        await asyncio.sleep(0.01)
        ticked.set()

    ticker_task = asyncio.create_task(ticker())
    endpoint_task = asyncio.create_task(awaitable)
    try:
        await asyncio.wait_for(ticked.wait(), timeout=0.05)
        return await endpoint_task
    finally:
        ticker_task.cancel()
        if not endpoint_task.done():
            endpoint_task.cancel()


def test_desktop_session_list_db_work_does_not_block_event_loop(monkeypatch):
    """Slow state.db listing must run off the FastAPI event loop.

    If /api/sessions executes SessionDB.list_sessions_rich inline, the 80ms
    blocking sleep prevents the 10ms ticker from firing and this test times out.
    """
    monkeypatch.setattr("hermes_state.SessionDB", _BlockingListSessionDB)

    response = asyncio.run(_assert_loop_stays_responsive(web_server.get_sessions(limit=1)))

    assert response["sessions"] == []
    assert response["total"] == 0


def test_desktop_session_search_db_work_does_not_block_event_loop(monkeypatch):
    """Slow FTS/id search work must also run off the FastAPI event loop."""
    monkeypatch.setattr("hermes_state.SessionDB", _BlockingSearchSessionDB)

    response = asyncio.run(
        _assert_loop_stays_responsive(web_server.search_sessions(q="20260603", limit=2))
    )

    assert response["results"][0]["session_id"] == "20260603_090200_exact"


def test_desktop_session_message_hydration_does_not_block_event_loop(monkeypatch):
    """Opening a transcript hydrates messages from state.db; keep it off-loop."""
    monkeypatch.setattr("hermes_state.SessionDB", _BlockingHydrationSessionDB)

    response = asyncio.run(
        _assert_loop_stays_responsive(web_server.get_session_messages("slow-session"))
    )

    assert response == {
        "session_id": "slow-session",
        "messages": [{"role": "user", "content": "hello"}],
    }
