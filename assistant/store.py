from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from .models import ACTIVE, Status, TaskConfig, now_iso


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, config TEXT NOT NULL, status TEXT NOT NULL,
                  message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL, result TEXT, submit_intent TEXT,
                  baseline TEXT, screenshot TEXT, submitted_at TEXT);
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                  time TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
            if "submitted_at" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN submitted_at TEXT")
            if "deleted_at" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN deleted_at TEXT")
            for column in ("payment_entered_at", "payment_order_id"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
            self.db.execute(
                "UPDATE tasks SET status=?, message=? WHERE status IN (%s)" %
                ",".join("?" for _ in ACTIVE),
                [Status.INTERRUPTED, "服务重启，需检查订单后恢复", *ACTIVE],
            )

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for field in ("config", "result", "baseline"):
            if result[field] is not None:
                result[field] = json.loads(result[field])
        return result

    def get(self, task_id: str, include_deleted=False):
        with self.lock:
            suffix = "" if include_deleted else " AND deleted_at IS NULL"
            return self.decode(self.db.execute("SELECT * FROM tasks WHERE id=?" + suffix, (task_id,)).fetchone())

    def tasks(self, deleted=False):
        with self.lock:
            clause = "IS NOT NULL" if deleted else "IS NULL"
            return [self.decode(row) for row in self.db.execute(f"SELECT * FROM tasks WHERE deleted_at {clause} ORDER BY created_at DESC")]

    def unresolved_submission(self, sku: str, exclude_id: str):
        with self.lock:
            for row in self.db.execute("SELECT * FROM tasks WHERE submit_intent IS NOT NULL AND id != ?", (exclude_id,)):
                task = self.decode(row)
                if task["config"]["product"]["sku"] == sku and (not task["result"] or task["result"]["outcome"] == "unknown"):
                    return task
        return None

    def create(self, config: TaskConfig):
        task_id, stamp = uuid.uuid4().hex, now_iso()
        with self.lock, self.db:
            self.db.execute("INSERT INTO tasks(id,config,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                            (task_id, config.model_dump_json(), Status.DRAFT, stamp, stamp))
        return self.get(task_id)

    def update(self, task_id: str, **fields):
        allowed = {"config", "status", "message", "result", "submit_intent", "baseline", "screenshot", "submitted_at", "payment_entered_at", "payment_order_id"}
        if not fields.keys() <= allowed:
            raise ValueError("Invalid task fields")
        values = {}
        for key, value in fields.items():
            values[key] = json.dumps(value, ensure_ascii=False) if key in {"config", "result", "baseline"} and value is not None else value
        values["updated_at"] = now_iso()
        with self.lock, self.db:
            self.db.execute("UPDATE tasks SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                            [*values.values(), task_id])
        return self.get(task_id)

    def delete(self, task_id: str):
        with self.lock, self.db:
            self.db.execute("UPDATE tasks SET deleted_at=? WHERE id=?", (now_iso(), task_id))

    def restore(self, task_id: str):
        with self.lock, self.db:
            self.db.execute("UPDATE tasks SET deleted_at=NULL WHERE id=?", (task_id,))
        return self.get(task_id)

    def event(self, task_id: str | None, message: str, level="info"):
        with self.lock, self.db:
            cursor = self.db.execute("INSERT INTO events(task_id,time,level,message) VALUES(?,?,?,?)",
                                     (task_id, now_iso(), level, message))
            return dict(self.db.execute("SELECT * FROM events WHERE id=?", (cursor.lastrowid,)).fetchone())

    def events(self, task_id: str | None = None, after=0):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM events WHERE id>? AND (? IS NULL OR task_id=?) AND NOT EXISTS "
                "(SELECT 1 FROM tasks WHERE tasks.id=events.task_id AND deleted_at IS NOT NULL) ORDER BY id DESC LIMIT 300",
                (after, task_id, task_id),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def close(self):
        self.db.close()
