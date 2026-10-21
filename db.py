
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path


class Database:
    """Persistent storage for AuthorMind novels and story memory."""

    def __init__(self, path=None):
        self.path = path or os.getenv(
            "DATABASE_PATH", "/data/authormind.sqlite3"
        )
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(
                parents=True, exist_ok=True
            )
        self.initialize()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self):
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                genre TEXT NOT NULL,
                premise TEXT NOT NULL,
                story_bible TEXT NOT NULL DEFAULT '',
                plan_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'planning',
                current_chapter INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_projects_chat
            ON projects(chat_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL
                    REFERENCES projects(id) ON DELETE CASCADE,
                chapter_no INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                outline TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned',
                scene_index INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                UNIQUE(project_id, chapter_no)
            );

            CREATE INDEX IF NOT EXISTS idx_chapters_project
            ON chapters(project_id, chapter_no);

            CREATE TABLE IF NOT EXISTS story_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL
                    REFERENCES projects(id) ON DELETE CASCADE,
                chapter_no INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'event',
                detail TEXT NOT NULL,
                cause TEXT NOT NULL DEFAULT '',
                consequences TEXT NOT NULL DEFAULT '',
                people_json TEXT NOT NULL DEFAULT '[]',
                unresolved INTEGER NOT NULL DEFAULT 0,
                importance INTEGER NOT NULL DEFAULT 3,
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_project
            ON story_events(project_id, chapter_no, id);

            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL
                    REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                profile TEXT NOT NULL DEFAULT '',
                current_state TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                UNIQUE(project_id, name)
            );

            CREATE TABLE IF NOT EXISTS story_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL
                    REFERENCES projects(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                introduced_chapter INTEGER NOT NULL DEFAULT 1,
                resolved_chapter INTEGER,
                updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_threads_project
            ON story_threads(project_id, status);
            """)

    @staticmethod
    def _dict(row):
        return dict(row) if row is not None else None

    def create_project(self, chat_id, title, genre, premise):
        now = int(time.time())
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO projects
                (chat_id,title,genre,premise,created_at,updated_at)
                VALUES(?,?,?,?,?,?)""",
                (chat_id, title.strip(), genre.strip(),
                 premise.strip(), now, now),
            )
            return cur.lastrowid

    def get_project(self, project_id):
        with self.connect() as c:
            return self._dict(c.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone())

    def latest_project(self, chat_id):
        with self.connect() as c:
            return self._dict(c.execute(
                """SELECT * FROM projects WHERE chat_id=?
                ORDER BY updated_at DESC,id DESC LIMIT 1""",
                (chat_id,),
            ).fetchone())

    def list_projects(self, chat_id):
        with self.connect() as c:
            return [dict(r) for r in c.execute(
                """SELECT * FROM projects WHERE chat_id=?
                ORDER BY updated_at DESC,id DESC""",
                (chat_id,),
            ).fetchall()]

    def save_plan(self, project_id, story_bible, plan):
        now = int(time.time())
        with self.connect() as c:
            c.execute(
                """UPDATE projects SET story_bible=?,plan_json=?,
                status='writing',updated_at=? WHERE id=?""",
                (story_bible, json.dumps(plan, ensure_ascii=False),
                 now, project_id),
            )
            for item in plan:
                n = int(item["number"])
                c.execute(
                    """INSERT INTO chapters
                    (project_id,chapter_no,title,outline,updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(project_id,chapter_no) DO UPDATE SET
                    title=excluded.title,outline=excluded.outline,
                    updated_at=excluded.updated_at""",
                    (project_id, n, item.get("title", f"Chapter {n}"),
                     item.get("outline", ""), now),
                )

    def get_plan(self, project_id):
        project = self.get_project(project_id)
        if not project:
            return []
        try:
            plan = json.loads(project["plan_json"] or "[]")
            return plan if isinstance(plan, list) else []
        except (ValueError, TypeError):
            return []

    def get_chapter(self, project_id, chapter_no):
        with self.connect() as c:
            return self._dict(c.execute(
                """SELECT * FROM chapters
                WHERE project_id=? AND chapter_no=?""",
                (project_id, chapter_no),
            ).fetchone())

    def list_chapters(self, project_id):
        with self.connect() as c:
            return [dict(r) for r in c.execute(
                """SELECT * FROM chapters WHERE project_id=?
                ORDER BY chapter_no""",
                (project_id,),
            ).fetchall()]

    def save_scene(self, project_id, chapter_no, scene_text,
                   scene_index, title=None):
        """Save a scene and its continuation checkpoint atomically."""
        now = int(time.time())
        with self.connect() as c:
            row = c.execute(
                """SELECT content,title FROM chapters
                WHERE project_id=? AND chapter_no=?""",
                (project_id, chapter_no),
            ).fetchone()
            if row is None:
                raise ValueError("Chapter has not been planned.")

            content = (
                row["content"].rstrip() + "\n\n" + scene_text.strip()
            ).strip()

            c.execute(
                """UPDATE chapters SET content=?,scene_index=?,
                status='in_progress',title=?,updated_at=?
                WHERE project_id=? AND chapter_no=?""",
                (content, scene_index, title or row["title"], now,
                 project_id, chapter_no),
            )
            c.execute(
                """UPDATE projects SET current_chapter=?,updated_at=?
                WHERE id=?""",
                (chapter_no, now, project_id),
            )

    def complete_chapter(self, project_id, chapter_no, summary):
        with self.connect() as c:
            c.execute(
                """UPDATE chapters SET status='complete',summary=?,
                updated_at=? WHERE project_id=? AND chapter_no=?""",
                (summary, int(time.time()), project_id, chapter_no),
            )

    def add_event(self, project_id, chapter_no, detail,
                  kind="event", cause="", consequences="",
                  people=None, unresolved=False, importance=3):
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO story_events
                (project_id,chapter_no,kind,detail,cause,consequences,
                people_json,unresolved,importance,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (project_id, chapter_no, kind, detail, cause,
                 consequences, json.dumps(people or [], ensure_ascii=False),
                 int(unresolved), max(1, min(5, int(importance))),
                 int(time.time())),
            )
            return cur.lastrowid

    def list_events(self, project_id, limit=500):
        with self.connect() as c:
            rows = c.execute(
                """SELECT * FROM story_events WHERE project_id=?
                ORDER BY id DESC LIMIT ?""",
                (project_id, max(1, min(int(limit), 5000))),
            ).fetchall()

        result = [dict(r) for r in reversed(rows)]
        for event in result:
            try:
                event["people"] = json.loads(
                    event.pop("people_json")
                )
            except (ValueError, TypeError):
                event["people"] = []
        return result

    def upsert_character(self, project_id, name,
                         profile="", current_state=""):
        now = int(time.time())
        with self.connect() as c:
            c.execute(
                """INSERT INTO characters
                (project_id,name,profile,current_state,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(project_id,name) DO UPDATE SET
                profile=CASE WHEN excluded.profile=''
                    THEN characters.profile ELSE excluded.profile END,
                current_state=CASE WHEN excluded.current_state=''
                    THEN characters.current_state
                    ELSE excluded.current_state END,
                updated_at=excluded.updated_at""",
                (project_id, name.strip(), profile,
                 current_state, now),
            )

    def list_characters(self, project_id):
        with self.connect() as c:
            return [dict(r) for r in c.execute(
                """SELECT * FROM characters WHERE project_id=?
                ORDER BY name""",
                (project_id,),
            ).fetchall()]

    def upsert_thread(self, project_id, title, description="",
                      status="open", introduced_chapter=1,
                      resolved_chapter=None):
        if status not in {"open", "resolved", "paused"}:
            raise ValueError("Invalid story-thread status.")

        now = int(time.time())
        with self.connect() as c:
            row = c.execute(
                """SELECT id FROM story_threads
                WHERE project_id=? AND title=?""",
                (project_id, title.strip()),
            ).fetchone()

            if row:
                c.execute(
                    """UPDATE story_threads SET description=?,status=?,
                    resolved_chapter=?,updated_at=? WHERE id=?""",
                    (description, status, resolved_chapter, now, row["id"]),
                )
            else:
                c.execute(
                    """INSERT INTO story_threads
                    (project_id,title,description,status,introduced_chapter,
                    resolved_chapter,updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (project_id, title.strip(), description, status,
                     introduced_chapter, resolved_chapter, now),
                )

    def list_threads(self, project_id, status=None):
        with self.connect() as c:
            if status:
                rows = c.execute(
                    """SELECT * FROM story_threads
                    WHERE project_id=? AND status=? ORDER BY id""",
                    (project_id, status),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT * FROM story_threads
                    WHERE project_id=? ORDER BY id""",
                    (project_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def set_project_status(self, project_id, status):
        allowed = {
            "planning", "writing", "editing",
            "complete", "paused", "error"
        }
        if status not in allowed:
            raise ValueError("Invalid project status.")

        with self.connect() as c:
            c.execute(
                """UPDATE projects SET status=?,updated_at=?
                WHERE id=?""",
                (status, int(time.time()), project_id),
            )
