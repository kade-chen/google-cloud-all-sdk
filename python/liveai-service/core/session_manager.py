import asyncio
import logging

logger = logging.getLogger(__name__)

class SessionManager:
    def __init__(self):
        self._sessions = {}   # {session_id: session_obj}
        self._connections = set()  # {websocket}
        self._lock = asyncio.Lock()

    async def add(self, websocket, session_id, session):
        async with self._lock:
            self._sessions[session_id] = session
            self._connections.add(websocket)
            self._log_status("added")

    async def remove(self, websocket, session_id):
        async with self._lock:
            self._sessions.pop(session_id, None)
            self._connections.discard(websocket)
            self._log_status("removed")

    def active_sessions(self):
        return len(self._sessions)

    def active_connections(self):
        return len(self._connections)

    def _log_status(self, action):
        logger.info(f"Session {action}. Active sessions={len(self._sessions)}, Active connections={len(self._connections)}")

# 全局实例
session_manager = SessionManager()
