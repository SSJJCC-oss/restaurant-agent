"""Firestore-backed SessionService for the restaurant order agent.

Persists ADK sessions to Firestore so conversation state survives Cloud Run
restarts/cold starts and is shared correctly across concurrent instances.

Scope: session-level state only (matches what this app actually uses today
— tools.py reads tool_context.session.user_id, no app/user-scoped state
keys). Does not replicate ADK's DatabaseSessionService's app-state/user-state
layering or its staleness-reload concurrency handling — add that later only
if a feature actually needs it.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from google.adk.events import Event
from google.adk.sessions import BaseSessionService, Session
# GetSessionConfig/ListSessionsResponse are no longer re-exported from the
# top-level google.adk.sessions package (confirmed against the installed
# version on 2026-09-19) — import them from base_session_service directly.
from google.adk.sessions.base_session_service import (
    GetSessionConfig,
    ListSessionsResponse,
)
from google.cloud import firestore

COLLECTION = "sessions"


class FirestoreSessionService(BaseSessionService):
    """Session persistence backed by Google Cloud Firestore."""

    def __init__(self, project: str | None = None, database: str = "(default)"):
        self._db = firestore.AsyncClient(project=project, database=database)

    def _doc_ref(self, app_name: str, user_id: str, session_id: str):
        doc_id = f"{app_name}__{user_id}__{session_id}"
        return self._db.collection(COLLECTION).document(doc_id)

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        session_id = session_id or str(uuid.uuid4())
        doc_ref = self._doc_ref(app_name, user_id, session_id)

        if (await doc_ref.get()).exists:
            raise ValueError(
                f"Session already exists: {app_name}/{user_id}/{session_id}"
            )

        now = time.time()
        await doc_ref.set(
            {
                "app_name": app_name,
                "user_id": user_id,
                "session_id": session_id,
                "state": state or {},
                "events": [],
                "created_at": now,
                "last_update_time": now,
            }
        )
        return Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=state or {},
            last_update_time=now,
        )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        doc = await self._doc_ref(app_name, user_id, session_id).get()
        if not doc.exists:
            return None

        data = doc.to_dict()
        events = [Event.model_validate(e) for e in data.get("events", [])]

        if config and config.num_recent_events:
            events = events[-config.num_recent_events :]
        elif config and config.after_timestamp:
            events = [e for e in events if e.timestamp > config.after_timestamp]

        return Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=data.get("state", {}),
            events=events,
            last_update_time=data.get("last_update_time", time.time()),
        )

    async def list_sessions(
        self, *, app_name: str, user_id: Optional[str] = None
    ) -> ListSessionsResponse:
        query = self._db.collection(COLLECTION).where("app_name", "==", app_name)
        if user_id is not None:
            query = query.where("user_id", "==", user_id)

        sessions = []
        async for doc in query.stream():
            data = doc.to_dict()
            sessions.append(
                Session(
                    app_name=data["app_name"],
                    user_id=data["user_id"],
                    id=data["session_id"],
                    state=data.get("state", {}),
                    last_update_time=data.get("last_update_time", time.time()),
                )
            )
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        await self._doc_ref(app_name, user_id, session_id).delete()

    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return event

        # Let the base class do the bookkeeping: append to session.events
        # and merge event.actions.state_delta into session.state, in place.
        await super().append_event(session=session, event=event)

        now = time.time()
        await self._doc_ref(session.app_name, session.user_id, session.id).update(
            {
                "state": session.state,
                "events": [e.model_dump(mode="json") for e in session.events],
                "last_update_time": now,
            }
        )
        session.last_update_time = now
        return event