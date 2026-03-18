from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import AlertEvent, TokenState


class SqliteStateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS token_state (
                dedupe_key TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS emitted_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL,
                ts_ms INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def get_state(self, dedupe_key: str) -> TokenState | None:
        row = self.conn.execute(
            "SELECT state_json FROM token_state WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row["state_json"])
        return TokenState.from_dict(data)

    def save_state(self, state: TokenState, updated_at_ms: int) -> None:
        payload = json.dumps(state.to_dict(), ensure_ascii=False)
        self.conn.execute(
            """
            INSERT INTO token_state(dedupe_key, state_json, updated_at_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at_ms = excluded.updated_at_ms
            """,
            (state.dedupe_key, payload, updated_at_ms),
        )
        self.conn.commit()

    def record_event(self, dedupe_key: str, event: AlertEvent) -> None:
        self.conn.execute(
            "INSERT INTO emitted_events(dedupe_key, ts_ms, event_type, event_json) VALUES (?, ?, ?, ?)",
            (dedupe_key, event.ts_ms, event.event, json.dumps(event.to_dict(), ensure_ascii=False)),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
