import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                trans_type TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT,
                description TEXT,
                record_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_trans_session_date
                ON transactions(session_id, record_date);
            CREATE INDEX IF NOT EXISTS idx_trans_type
                ON transactions(session_id, trans_type);

            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                note_type TEXT NOT NULL,
                title TEXT,
                raw_content TEXT NOT NULL,
                polished_content TEXT,
                record_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_notes_session_type
                ON notes(session_id, note_type);
            CREATE INDEX IF NOT EXISTS idx_notes_title
                ON notes(session_id, title);

            CREATE TABLE IF NOT EXISTS media_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'want',
                rating REAL,
                note TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                UNIQUE(session_id, media_type, title)
            );
            CREATE INDEX IF NOT EXISTS idx_media_session_type
                ON media_items(session_id, media_type);
            CREATE INDEX IF NOT EXISTS idx_media_status
                ON media_items(session_id, status);

            CREATE TABLE IF NOT EXISTS health_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                metric_type TEXT NOT NULL,
                value REAL,
                value_text TEXT,
                note TEXT,
                record_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_health_session_date
                ON health_logs(session_id, record_date);

            CREATE TABLE IF NOT EXISTS conversation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                images TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_conv_session
                ON conversation_logs(session_id, created_at);

            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                schedule_type TEXT NOT NULL DEFAULT 'one_time',
                start_time TEXT NOT NULL,
                end_time TEXT,
                location TEXT,
                priority TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'pending',
                remind_before INTEGER NOT NULL DEFAULT 15,
                remind_at TEXT,
                recurring_rule TEXT,
                recurring_rule_desc TEXT,
                tags TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_schedule_session
                ON schedule(session_id, start_time);
            CREATE INDEX IF NOT EXISTS idx_schedule_status
                ON schedule(session_id, status);

            CREATE TABLE IF NOT EXISTS reminder_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL,
                reminded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminder_schedule
                ON reminder_log(schedule_id);
        """)
        self.conn.commit()

    async def add_transaction(
        self, session_id: str, trans_type: str, amount: float,
        category: str = None, description: str = None,
        record_date: str = None,
    ) -> int:
        if record_date is None:
            record_date = datetime.now().strftime("%Y-%m-%d")
        async with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO transactions "
                "(session_id, trans_type, amount, category, description, record_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, trans_type, amount, category, description, record_date),
            )
            self.conn.commit()
            return cursor.lastrowid

    async def query_transactions(
        self, session_id: str, trans_type: str = None,
        category: str = None, start_date: str = None,
        end_date: str = None, limit: int = 50,
    ) -> list[dict]:
        query = "SELECT * FROM transactions WHERE session_id = ?"
        params: list[Any] = [session_id]
        if trans_type:
            query += " AND trans_type = ?"
            params.append(trans_type)
        if category:
            query += " AND category = ?"
            params.append(category)
        if start_date:
            query += " AND record_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND record_date <= ?"
            params.append(end_date)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        cols = [
            "id", "session_id", "trans_type", "amount", "category",
            "description", "record_date", "created_at",
        ]
        return [dict(zip(cols, row)) for row in rows]

    async def get_financial_summary(
        self, session_id: str, period: str = "month",
    ) -> dict:
        now = datetime.now()
        if period == "today":
            start = now.strftime("%Y-%m-%d")
        elif period == "week":
            start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        elif period == "month":
            start = now.strftime("%Y-%m-01")
        elif period == "year":
            start = now.strftime("%Y-01-01")
        else:
            start = "2000-01-01"

        async with self._lock:
            expense_row = self.conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM transactions "
                "WHERE session_id = ? AND trans_type = 'expense' "
                "AND record_date >= ?",
                (session_id, start),
            ).fetchone()
            income_row = self.conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM transactions "
                "WHERE session_id = ? AND trans_type = 'income' "
                "AND record_date >= ?",
                (session_id, start),
            ).fetchone()
            latest_asset = self.conn.execute(
                "SELECT amount, record_date FROM transactions "
                "WHERE session_id = ? AND trans_type = 'asset' "
                "ORDER BY record_date DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            categories = self.conn.execute(
                "SELECT category, SUM(amount) as total FROM transactions "
                "WHERE session_id = ? AND trans_type = 'expense' "
                "AND record_date >= ? GROUP BY category ORDER BY total DESC",
                (session_id, start),
            ).fetchall()

        return {
            "period": period,
            "start_date": start,
            "total_expense": expense_row[0],
            "total_income": income_row[0],
            "net": income_row[0] - expense_row[0],
            "latest_asset": (
                {"amount": latest_asset[0], "date": latest_asset[1]}
                if latest_asset else None
            ),
            "category_breakdown": [
                {"category": r[0], "total": r[1]} for r in categories
            ],
        }

    async def add_note(
        self, session_id: str, note_type: str, title: str,
        raw_content: str, polished_content: str = None,
        record_date: str = None,
    ) -> int:
        if record_date is None:
            record_date = datetime.now().strftime("%Y-%m-%d")
        async with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO notes "
                "(session_id, note_type, title, raw_content, "
                "polished_content, record_date) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, note_type, title, raw_content,
                 polished_content, record_date),
            )
            self.conn.commit()
            return cursor.lastrowid

    async def query_notes(
        self, session_id: str, note_type: str = None,
        title: str = None, days: int = 30, limit: int = 20,
    ) -> list[dict]:
        query = "SELECT * FROM notes WHERE session_id = ?"
        params: list[Any] = [session_id]
        if note_type:
            query += " AND note_type = ?"
            params.append(note_type)
        if title:
            query += " AND title LIKE ?"
            params.append(f"%{title}%")
        if days > 0:
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            query += " AND record_date >= ?"
            params.append(since)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        cols = [
            "id", "session_id", "note_type", "title", "raw_content",
            "polished_content", "record_date", "created_at",
        ]
        return [dict(zip(cols, row)) for row in rows]

    async def search_notes(
        self, session_id: str, query: str,
        note_type: str = None, limit: int = 10,
    ) -> list[dict]:
        sql = (
            "SELECT id, note_type, title, record_date, "
            "SUBSTR(raw_content, 1, 200) AS snippet "
            "FROM notes WHERE session_id = ? "
            "AND (title LIKE ? OR raw_content LIKE ?)"
        )
        kw = f"%{query}%"
        params: list[Any] = [session_id, kw, kw]
        if note_type:
            sql += " AND note_type = ?"
            params.append(note_type)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        cols = ["id", "note_type", "title", "record_date", "snippet"]
        return [dict(zip(cols, row)) for row in rows]

    async def add_media_item(
        self, session_id: str, media_type: str,
        title: str, status: str = "want",
    ) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._lock:
            try:
                cursor = self.conn.execute(
                    "INSERT INTO media_items "
                    "(session_id, media_type, title, status, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, media_type, title, status, now, now),
                )
                self.conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                return -1

    async def update_media_item(
        self, session_id: str, media_type: str,
        title: str, status: str = None, rating: float = None,
        note: str = None,
    ) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sets = ["updated_at = ?"]
        params: list[Any] = [now]
        if status:
            sets.append("status = ?")
            params.append(status)
            if status == "doing":
                sets.append("started_at = ?")
                params.append(now[:10])
            elif status == "done":
                sets.append("finished_at = ?")
                params.append(now[:10])
        if rating is not None:
            sets.append("rating = ?")
            params.append(rating)
        if note:
            sets.append("note = ?")
            params.append(note)
        params.extend([session_id, media_type, title])
        async with self._lock:
            cursor = self.conn.execute(
                f"UPDATE media_items SET {', '.join(sets)} "
                "WHERE session_id = ? AND media_type = ? AND title = ?",
                params,
            )
            self.conn.commit()
            return cursor.rowcount > 0

    async def query_media_items(
        self, session_id: str, media_type: str = None,
        status: str = None,
    ) -> list[dict]:
        query = "SELECT * FROM media_items WHERE session_id = ?"
        params: list[Any] = [session_id]
        if media_type:
            query += " AND media_type = ?"
            params.append(media_type)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC"
        async with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        cols = [
            "id", "session_id", "media_type", "title", "status",
            "rating", "note", "started_at", "finished_at",
            "created_at", "updated_at",
        ]
        return [dict(zip(cols, row)) for row in rows]

    async def add_health_log(
        self, session_id: str, metric_type: str,
        value: float = None, value_text: str = None,
        note: str = None, record_date: str = None,
    ) -> int:
        if record_date is None:
            record_date = datetime.now().strftime("%Y-%m-%d")
        async with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO health_logs "
                "(session_id, metric_type, value, value_text, "
                "note, record_date) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, metric_type, value, value_text, note,
                 record_date),
            )
            self.conn.commit()
            return cursor.lastrowid

    async def query_health_logs(
        self, session_id: str, metric_type: str = None,
        days: int = 30, limit: int = 50,
    ) -> list[dict]:
        query = "SELECT * FROM health_logs WHERE session_id = ?"
        params: list[Any] = [session_id]
        if metric_type:
            query += " AND metric_type = ?"
            params.append(metric_type)
        if days > 0:
            since = (datetime.now() - timedelta(days=days)).strftime(
                "%Y-%m-%d"
            )
            query += " AND record_date >= ?"
            params.append(since)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        cols = [
            "id", "session_id", "metric_type", "value", "value_text",
            "note", "record_date", "created_at",
        ]
        return [dict(zip(cols, row)) for row in rows]

    async def get_profile(self, session_id: str, profile_type: str) -> str | None:
        async with self._lock:
            row = self.conn.execute(
                "SELECT raw_content FROM notes "
                "WHERE session_id = ? AND note_type = 'profile' "
                "AND title LIKE ? ORDER BY id DESC LIMIT 1",
                (session_id, f"%{profile_type}%"),
            ).fetchone()
        return row[0] if row else None

    async def update_profile(
        self, session_id: str, profile_type: str, new_content: str,
    ) -> bool:
        async with self._lock:
            cursor = self.conn.execute(
                "UPDATE notes SET raw_content = ? "
                "WHERE session_id = ? AND note_type = 'profile' "
                "AND title LIKE ?",
                (new_content, session_id, f"%{profile_type}%"),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    async def add_conversation_log(
        self, session_id: str, role: str, content: str, images: str = "",
    ) -> int:
        async with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO conversation_logs "
                "(session_id, role, content, images) VALUES (?, ?, ?, ?)",
                (session_id, role, content, images),
            )
            self.conn.commit()
            return cursor.lastrowid

    async def query_conversation_logs(
        self, session_id: str, date: str = None,
    ) -> list[dict]:
        query = "SELECT * FROM conversation_logs WHERE session_id = ?"
        params: list[Any] = [session_id]
        if date:
            query += " AND DATE(created_at) = ?"
            params.append(date)
        query += " ORDER BY id ASC"
        async with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        cols = ["id", "session_id", "role", "content", "images", "created_at"]
        return [dict(zip(cols, row)) for row in rows]

    async def clear_conversation_logs(self, session_id: str, date: str = None):
        if date:
            async with self._lock:
                self.conn.execute(
                    "DELETE FROM conversation_logs "
                    "WHERE session_id = ? AND DATE(created_at) = ?",
                    (session_id, date),
                )
                self.conn.commit()
        else:
            async with self._lock:
                self.conn.execute(
                    "DELETE FROM conversation_logs WHERE session_id = ?",
                    (session_id,),
                )
                self.conn.commit()

    async def has_conversation_logs(self, session_id: str, date: str) -> bool:
        async with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM conversation_logs "
                "WHERE session_id = ? AND DATE(created_at) = ?",
                (session_id, date),
            ).fetchone()
        return row[0] > 0

    def close(self):
        self.conn.close()

    async def insert_schedule(
        self, session_id: str, title: str, start_time: str,
        description: str = None, schedule_type: str = "one_time",
        end_time: str = None, location: str = None,
        priority: str = "medium", remind_before: int = 15,
        remind_at: str = None, recurring_rule: str = None,
        recurring_rule_desc: str = None, tags: str = None,
    ) -> int:
        async with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO schedule "
                "(session_id, title, description, schedule_type, start_time, "
                "end_time, location, priority, remind_before, remind_at, "
                "recurring_rule, recurring_rule_desc, tags) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, title, description, schedule_type, start_time,
                 end_time, location, priority, remind_before, remind_at,
                 recurring_rule, recurring_rule_desc, tags),
            )
            self.conn.commit()
            return cursor.lastrowid

    _SCHEDULE_UPDATABLE_COLS = frozenset({
        "title", "description", "schedule_type", "start_time",
        "end_time", "location", "priority", "status", "remind_before",
        "remind_at", "recurring_rule", "recurring_rule_desc", "tags",
    })

    _SCHEDULE_COLS = [
        "id", "session_id", "title", "description", "schedule_type",
        "start_time", "end_time", "location", "priority", "status",
        "remind_before", "remind_at", "recurring_rule",
        "recurring_rule_desc", "tags", "created_at", "updated_at",
    ]

    def _schedule_row_to_dict(self, row) -> dict:
        return dict(zip(self._SCHEDULE_COLS, row))

    async def update_schedule(self, schedule_id: int, **kwargs) -> bool:
        if not kwargs:
            return False
        filtered = {k: v for k, v in kwargs.items() if k in self._SCHEDULE_UPDATABLE_COLS}
        if not filtered:
            return False
        sets = []
        params = []
        for k, v in filtered.items():
            sets.append(f"{k} = ?")
            params.append(v)
        params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        sets.append("updated_at = ?")
        params.append(schedule_id)
        async with self._lock:
            cursor = self.conn.execute(
                f"UPDATE schedule SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            self.conn.commit()
            return cursor.rowcount > 0

    async def delete_schedule(self, schedule_id: int) -> bool:
        async with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM schedule WHERE id = ?", (schedule_id,),
            )
            self.conn.execute(
                "DELETE FROM reminder_log WHERE schedule_id = ?",
                (schedule_id,),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    async def query_schedules(
        self, session_id: str, start_date: str = None,
        end_date: str = None, status: str = None,
        tag: str = None, priority: str = None,
        schedule_type: str = None,
    ) -> list[dict]:
        query = "SELECT * FROM schedule WHERE session_id = ?"
        params: list[Any] = [session_id]
        if start_date:
            query += " AND start_time >= ?"
            params.append(start_date)
        if end_date:
            query += " AND start_time < ?"
            params.append(end_date)
        if status:
            query += " AND status = ?"
            params.append(status)
        if tag:
            query += " AND tags LIKE ?"
            params.append(f"%{tag}%")
        if priority:
            query += " AND priority = ?"
            params.append(priority)
        if schedule_type:
            query += " AND schedule_type = ?"
            params.append(schedule_type)
        query += " ORDER BY start_time"
        async with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [self._schedule_row_to_dict(r) for r in rows]

    async def get_schedule(self, schedule_id: int) -> dict | None:
        async with self._lock:
            row = self.conn.execute(
                "SELECT * FROM schedule WHERE id = ?", (schedule_id,),
            ).fetchone()
        if not row:
            return None
        return self._schedule_row_to_dict(row)

    async def get_pending_reminders(self, session_id: str, now_iso: str) -> list[dict]:
        async with self._lock:
            rows = self.conn.execute(
                "SELECT s.* FROM schedule s "
                "WHERE s.session_id = ? AND s.status = 'pending' "
                "AND s.remind_at IS NOT NULL AND s.remind_at <= ? "
                "AND s.id NOT IN (SELECT schedule_id FROM reminder_log "
                "  WHERE schedule_id = s.id AND reminded_at >= s.remind_at)",
                (session_id, now_iso),
            ).fetchall()
        return [self._schedule_row_to_dict(r) for r in rows]

    async def log_reminder(self, schedule_id: int, reminded_at: str):
        async with self._lock:
            self.conn.execute(
                "INSERT INTO reminder_log (schedule_id, reminded_at) VALUES (?, ?)",
                (schedule_id, reminded_at),
            )
            self.conn.commit()

    async def get_all_pending_with_remind(self, now_iso: str) -> list[dict]:
        async with self._lock:
            rows = self.conn.execute(
                "SELECT s.* FROM schedule s "
                "WHERE s.status = 'pending' "
                "AND s.remind_at IS NOT NULL AND s.remind_at <= ? "
                "AND s.id NOT IN (SELECT schedule_id FROM reminder_log "
                "  WHERE schedule_id = s.id AND reminded_at >= s.remind_at)",
                (now_iso,),
            ).fetchall()
        return [self._schedule_row_to_dict(r) for r in rows]
