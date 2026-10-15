"""ADK session service backed by Firestore - diagram edge "Session State" -> Firestore.

ADK ships in-memory, SQL and Vertex AI session services; none of them fit the free
tier, so this implements `BaseSessionService` on Firestore Native mode:

  adk_apps/{app}                                    {state_json}            (app:  state)
  adk_apps/{app}/users/{user}                       {state_json}            (user: state)
  adk_apps/{app}/users/{user}/sessions/{sid}        {state_json, last_update_time, expire_at}
  adk_apps/{app}/users/{user}/sessions/{sid}/events/{eid}  {event_json, timestamp, expire_at}

State is stored as a JSON string so arbitrary keys (`acs:principal`, `user:x`) never
collide with Firestore field-path syntax. `expire_at` drives Firestore TTL policies
(configured in Terraform) so abandoned sessions are purged automatically.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, override

from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.events.event import Event
from google.adk.sessions import _session_util
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session
from google.adk.sessions.state import State

_ID_RE = re.compile(r"^[A-Za-z0-9_.:@|-]{1,256}$")


def _check_id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _ID_RE.match(value) or value in (".", ".."):
        raise ValueError(f"invalid {what}")
    return value


def _dumps(state: dict[str, Any]) -> str:
    return json.dumps(_session_util.make_json_safe_state(state))


def _loads(raw: str | None) -> dict[str, Any]:
    return json.loads(raw) if raw else {}


def _snap_state(snap: Any) -> dict[str, Any]:
    return _loads((snap.to_dict() or {}).get("state_json"))


class FirestoreSessionService(BaseSessionService):
    def __init__(self, db: Any, *, ttl_days: int = 7, root_collection: str = "adk_apps") -> None:
        self._db = db
        self._ttl = timedelta(days=ttl_days)
        self._root = root_collection

    # ------------------------------------------------------------------ refs
    def _app_ref(self, app_name: str) -> Any:
        return self._db.collection(self._root).document(_check_id(app_name, "app_name"))

    def _user_ref(self, app_name: str, user_id: str) -> Any:
        return self._app_ref(app_name).collection("users").document(_check_id(user_id, "user_id"))

    def _session_ref(self, app_name: str, user_id: str, session_id: str) -> Any:
        return (
            self._user_ref(app_name, user_id)
            .collection("sessions")
            .document(_check_id(session_id, "session_id"))
        )

    def _expire_at(self) -> datetime:
        return datetime.now(UTC) + self._ttl

    async def _scoped_state(self, app_name: str, user_id: str) -> dict[str, Any]:
        app_snap = await self._app_ref(app_name).get()
        user_snap = await self._user_ref(app_name, user_id).get()
        merged: dict[str, Any] = {}
        if app_snap.exists:
            for k, v in _snap_state(app_snap).items():
                merged[State.APP_PREFIX + k] = v
        if user_snap.exists:
            for k, v in _snap_state(user_snap).items():
                merged[State.USER_PREFIX + k] = v
        return merged

    async def _merge_scoped(self, ref: Any, delta: dict[str, Any], transaction: Any) -> None:
        snap = await ref.get(transaction=transaction)
        current = _snap_state(snap) if snap.exists else {}
        current.update(delta)
        transaction.set(ref, {"state_json": _dumps(current)}, merge=True)

    # ------------------------------------------------------------------ API
    @override
    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        from google.api_core.exceptions import AlreadyExists

        session_id = _check_id(session_id, "session_id") if session_id else uuid.uuid4().hex
        deltas = _session_util.extract_state_delta(state or {})
        now = time.time()
        ref = self._session_ref(app_name, user_id, session_id)
        try:
            await ref.create(
                {
                    "app_name": app_name,
                    "user_id": user_id,
                    "state_json": _dumps(deltas["session"]),
                    "last_update_time": now,
                    "expire_at": self._expire_at(),
                }
            )
        except AlreadyExists as exc:
            raise AlreadyExistsError(f"Session with id {session_id} already exists.") from exc

        user_doc: dict[str, Any] = {"touched_at": datetime.now(UTC)}
        if deltas["user"] or deltas["app"]:
            transaction = self._db.transaction()

            async def _apply(tx: Any) -> None:
                if deltas["app"]:
                    await self._merge_scoped(self._app_ref(app_name), deltas["app"], tx)
                if deltas["user"]:
                    await self._merge_scoped(self._user_ref(app_name, user_id), deltas["user"], tx)

            await self._run_transaction(transaction, _apply)
        await self._user_ref(app_name, user_id).set(user_doc, merge=True)

        session_state = dict(deltas["session"])
        session_state.update(await self._scoped_state(app_name, user_id))
        return Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=session_state,
            last_update_time=now,
        )

    @override
    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        ref = self._session_ref(app_name, user_id, session_id)
        snap = await ref.get()
        if not snap.exists:
            return None
        data = snap.to_dict()

        query = ref.collection("events")
        if config and config.after_timestamp is not None:
            query = query.where("timestamp", ">=", config.after_timestamp)
        if config and config.num_recent_events is not None:
            if config.num_recent_events == 0:
                docs = []
            else:
                desc = query.order_by("timestamp", direction="DESCENDING").limit(
                    config.num_recent_events
                )
                docs = list(reversed([d async for d in desc.stream()]))
        else:
            docs = [d async for d in query.order_by("timestamp").stream()]

        events = [Event.model_validate_json(d.get("event_json")) for d in docs]
        state = _loads(data.get("state_json"))
        state.update(await self._scoped_state(app_name, user_id))
        return Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=state,
            events=events,
            last_update_time=data.get("last_update_time", 0.0),
        )

    @override
    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> ListSessionsResponse:
        if user_id is not None:
            user_ids = [user_id]
        else:
            user_ids = [u.id async for u in self._app_ref(app_name).collection("users").stream()]
        sessions: list[Session] = []
        for uid in user_ids:
            scoped = await self._scoped_state(app_name, uid)
            async for doc in self._user_ref(app_name, uid).collection("sessions").stream():
                data = doc.to_dict()
                state = _loads(data.get("state_json"))
                state.update(scoped)
                sessions.append(
                    Session(
                        id=doc.id,
                        app_name=app_name,
                        user_id=uid,
                        state=state,
                        last_update_time=data.get("last_update_time", 0.0),
                    )
                )
        sessions.sort(key=lambda s: (s.last_update_time, s.user_id, s.id))
        return ListSessionsResponse(sessions=sessions)

    @override
    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        ref = self._session_ref(app_name, user_id, session_id)
        while True:
            docs = [d async for d in ref.collection("events").limit(400).stream()]
            if not docs:
                break
            batch = self._db.batch()
            for d in docs:
                batch.delete(d.reference)
            await batch.commit()
        await ref.delete()

    @override
    async def get_user_state(self, *, app_name: str, user_id: str) -> dict[str, Any]:
        snap = await self._user_ref(app_name, user_id).get()
        return _snap_state(snap) if snap.exists else {}

    @override
    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return event
        session_ref = self._session_ref(session.app_name, session.user_id, session.id)
        if not (await session_ref.get()).exists:
            raise SessionNotFoundError(f"Session {session.id} not found.")

        # Base class applies temp state in memory, trims temp keys and appends the event.
        event = await super().append_event(session=session, event=event)
        session.last_update_time = event.timestamp

        deltas = _session_util.extract_state_delta(
            event.actions.state_delta if event.actions and event.actions.state_delta else {}
        )
        event_ref = session_ref.collection("events").document(_check_id(event.id, "event_id"))
        event_json = event.model_dump_json(exclude_none=True)
        expire_at = self._expire_at()
        transaction = self._db.transaction()

        async def _apply(tx: Any) -> None:
            # All reads must happen before writes inside a Firestore transaction.
            snap = await session_ref.get(transaction=tx)
            app_ref = self._app_ref(session.app_name)
            user_ref = self._user_ref(session.app_name, session.user_id)
            app_snap = await app_ref.get(transaction=tx) if deltas["app"] else None
            user_snap = await user_ref.get(transaction=tx) if deltas["user"] else None

            stored = _snap_state(snap) if snap.exists else {}
            stored.update(deltas["session"])
            tx.set(
                event_ref,
                {"event_json": event_json, "timestamp": event.timestamp, "expire_at": expire_at},
            )
            tx.update(
                session_ref,
                {
                    "state_json": _dumps(stored),
                    "last_update_time": event.timestamp,
                    "expire_at": expire_at,
                },
            )
            if app_snap is not None:
                app_state = _snap_state(app_snap) if app_snap.exists else {}
                app_state.update(deltas["app"])
                tx.set(app_ref, {"state_json": _dumps(app_state)}, merge=True)
            if user_snap is not None:
                user_state = _snap_state(user_snap) if user_snap.exists else {}
                user_state.update(deltas["user"])
                tx.set(user_ref, {"state_json": _dumps(user_state)}, merge=True)

        await self._run_transaction(transaction, _apply)
        return event

    @staticmethod
    async def _run_transaction(transaction: Any, fn: Any) -> None:
        from google.cloud.firestore import async_transactional

        @async_transactional
        async def _wrapped(tx: Any) -> None:
            await fn(tx)

        await _wrapped(transaction)
