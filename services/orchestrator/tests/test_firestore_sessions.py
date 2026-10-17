"""FirestoreSessionService against the Firestore emulator.

Run with:  gcloud emulators firestore start --host-port=localhost:8681
           FIRESTORE_EMULATOR_HOST=localhost:8681 uv run pytest tests/test_firestore_sessions.py
"""

from __future__ import annotations

import os
import uuid

import pytest
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions.base_session_service import GetSessionConfig
from google.genai import types

pytestmark = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_EMULATOR_HOST"), reason="Firestore emulator not running"
)


@pytest.fixture
def service():
    from google.cloud import firestore

    from acs_orchestrator.sessions.firestore_session_service import FirestoreSessionService

    db = firestore.AsyncClient(project="acs-test")
    return FirestoreSessionService(db, root_collection=f"t_{uuid.uuid4().hex[:8]}")


async def test_round_trip(service):
    s = await service.create_session(
        app_name="app", user_id="m1:merchant_user:u1", state={"k": 1, "user:pref": "x"}
    )
    assert s.state == {"k": 1, "user:pref": "x"}

    for i in range(3):
        event = Event(
            author="user",
            invocation_id=f"inv{i}",
            content=types.Content(role="user", parts=[types.Part(text=f"msg {i}")]),
            actions=EventActions(state_delta={"k": i + 10, "temp:t": 1, "app:flag": True}),
        )
        await service.append_event(s, event)

    loaded = await service.get_session(
        app_name="app", user_id="m1:merchant_user:u1", session_id=s.id
    )
    assert [e.content.parts[0].text for e in loaded.events] == ["msg 0", "msg 1", "msg 2"]
    assert loaded.state["k"] == 12
    assert loaded.state["app:flag"] is True
    assert loaded.state["user:pref"] == "x"
    assert "temp:t" not in loaded.state

    recent = await service.get_session(
        app_name="app",
        user_id="m1:merchant_user:u1",
        session_id=s.id,
        config=GetSessionConfig(num_recent_events=1),
    )
    assert [e.content.parts[0].text for e in recent.events] == ["msg 2"]

    # Other users cannot see the session.
    assert (
        await service.get_session(app_name="app", user_id="m2:merchant_user:u1", session_id=s.id)
        is None
    )
    listed = await service.list_sessions(app_name="app", user_id="m1:merchant_user:u1")
    assert [x.id for x in listed.sessions] == [s.id]

    await service.delete_session(app_name="app", user_id="m1:merchant_user:u1", session_id=s.id)
    assert (
        await service.get_session(app_name="app", user_id="m1:merchant_user:u1", session_id=s.id)
        is None
    )


async def test_rejects_path_injection(service):
    with pytest.raises(ValueError):
        await service.create_session(app_name="app", user_id="../other", state={})
