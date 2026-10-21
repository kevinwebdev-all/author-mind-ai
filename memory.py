
"""AuthorMind story memory: canon, consequences, characters, and plot threads."""
from __future__ import annotations

import json
import re
from typing import Any

from db import Database
from providers import ModelProvider


MEMORY_SYSTEM = """You maintain continuity for a serious novel.
Extract only facts supported by the manuscript. Never invent canon.
Track what happened, why it happened, its consequences, who knows what,
changes in relationships and motivations, promises, secrets, injuries,
possessions, timeline facts, and unresolved plot threads.
Distinguish confirmed facts from suspicion, belief, and deception.
Be precise and concise. Output valid JSON only."""


class StoryMemory:
    def __init__(
        self,
        database: Database,
        provider: ModelProvider,
    ):
        self.db = database
        self.ai = provider

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """Parse JSON even if a model wraps it in Markdown fences."""
        text = text.strip()
        text = re.sub(
            r"\A```(?:json)?\s*|\s*```\Z",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("Model did not return a JSON object.")
            value = json.loads(text[start:end + 1])

        if not isinstance(value, dict):
            raise ValueError("Memory response must be a JSON object.")

        return value

    @staticmethod
    def _clean(value: Any, limit: int = 2000) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()[:limit]

    @staticmethod
    def _terms(text: str) -> set[str]:
        common = {
            "about", "after", "again", "also", "because", "before",
            "being", "could", "does", "each", "from", "have", "into",
            "just", "more", "most", "other", "over", "same", "should",
            "some", "such", "than", "that", "their", "them", "then",
            "there", "these", "they", "this", "those", "through", "under",
            "until", "very", "was", "were", "what", "when", "where",
            "which", "while", "with", "would", "your", "chapter", "scene",
        }
        return {
            word.lower()
            for word in re.findall(r"[A-Za-z0-9']{3,}", text)
            if word.lower() not in common
        }

    def record_chapter(
        self,
        project_id: int,
        chapter_no: int,
        chapter_text: str,
    ) -> dict[str, int]:
        """Extract durable canon from a completed chapter and save it.

        Call this after a chapter has been written and checked.
        """
        if not chapter_text or len(chapter_text.strip()) < 100:
            raise ValueError(
                "Chapter text is too short to extract reliable story memory."
            )

        project = self.db.get_project(project_id)
        if not project:
            raise ValueError(f"Project {project_id} does not exist.")

        prompt = f"""Extract the durable story canon from this chapter.

Return one JSON object with these keys:

"summary": a concise summary of the chapter.
"events": an array of objects containing:
  "kind": event category,
  "detail": the confirmed event or fact,
  "cause": what caused it, if established,
  "consequences": immediate or likely consequences supported by the text,
  "people": names of people involved,
  "unresolved": true only if a consequence or question remains open,
  "importance": integer from 1 to 5.

"characters": an array of objects containing:
  "name": character name,
  "profile": enduring traits or motivations newly established,
  "current_state": current emotional, physical, relational, or situational state.

"threads": an array of objects containing:
  "title": short, distinctive plot-thread name,
  "description": what remains unresolved,
  "status": "open", "resolved", or "paused".

"knowledge": an array of objects containing:
  "person": character name,
  "fact": what that character knows or believes,
  "certainty": "confirmed", "believed", or "suspected".

Rules:
- Include meaningful events, not every trivial action.
- Preserve cause and effect.
- Do not treat a character's lie as objective truth.
- Record changes, not unsupported assumptions.
- Do not mark a thread resolved unless the text resolves it.
- Do not overwrite earlier canon merely because a character is mistaken.
- Use an empty array when there is no supported information.
- Return valid JSON only.

BOOK: {project['title']}
CHAPTER: {chapter_no}

MANUSCRIPT:
{chapter_text[-18000:]}
"""

        raw = self.ai.complete(
            [
                {"role": "system", "content": MEMORY_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=3000,
            temperature=0.1,
        )
        data = self._parse_json(raw)

        events = data.get("events", [])
        characters = data.get("characters", [])
        threads = data.get("threads", [])

        if not all(isinstance(x, list) for x in
                   (events, characters, threads)):
            raise ValueError("Memory extraction returned invalid arrays.")

        # Validate the entire extraction before writing anything.
        for event in events:
            if not isinstance(event, dict) or not self._clean(
                event.get("detail")
            ):
                raise ValueError("An extracted event has no valid detail.")

        for character in characters:
            if not isinstance(character, dict) or not self._clean(
                character.get("name", ""), 200
            ):
                raise ValueError("An extracted character has no valid name.")

        for thread in threads:
            if not isinstance(thread, dict) or not self._clean(
                thread.get("title", ""), 200
            ):
                raise ValueError("An extracted thread has no valid title.")
            if thread.get("status", "open") not in {
                "open", "resolved", "paused"
            }:
                raise ValueError("An extracted thread has an invalid status.")

        # Persist facts. Existing project data remains intact if extraction fails.
        saved_events = 0
        saved_characters = 0
        saved_threads = 0

        for event in events:
            importance = event.get("importance", 3)
            try:
                importance = max(1, min(5, int(importance)))
            except (TypeError, ValueError):
                importance = 3

            people = event.get("people", [])
            if not isinstance(people, list):
                people = []

            self.db.add_event(
                project_id=project_id,
                chapter_no=chapter_no,
                kind=self._clean(event.get("kind", "event"), 100) or "event",
                detail=self._clean(event["detail"]),
                cause=self._clean(event.get("cause")),
                consequences=self._clean(event.get("consequences")),
                people=[
                    self._clean(p, 200)
                    for p in people
                    if isinstance(p, str) and p.strip()
                ],
                unresolved=bool(event.get("unresolved", False)),
                importance=importance,
            )
            saved_events += 1

        for character in characters:
            self.db.upsert_character(
                project_id=project_id,
                name=self._clean(character["name"], 200),
                profile=self._clean(character.get("profile")),
                current_state=self._clean(character.get("current_state")),
            )
            saved_characters += 1

        for thread in threads:
            title = self._clean(thread["title"], 200)
            status = thread.get("status", "open")

            existing = [
                t for t in self.db.list_threads(project_id)
                if t["title"].casefold() == title.casefold()
            ]
            previous = existing[0] if existing else None

            resolved_chapter = (
                chapter_no if status == "resolved"
                else (
                    previous.get("resolved_chapter")
                    if previous else None
                )
            )

            # Preserve the original introduction chapter for existing threads.
            introduced = (
                previous["introduced_chapter"]
                if previous else chapter_no
            )

            self.db.upsert_thread(
                project_id=project_id,
                title=title,
                description=self._clean(thread.get("description")),
                status=status,
                introduced_chapter=introduced,
                resolved_chapter=resolved_chapter,
            )
            saved_threads += 1

        # Knowledge statements are returned for audit, not treated as
        # objective facts. A later engine can use them to build POV-aware memory.
        knowledge = data.get("knowledge", [])
        if not isinstance(knowledge, list):
            knowledge = []

        return {
            "events": saved_events,
            "characters": saved_characters,
            "threads": saved_threads,
            "knowledge_statements": len(knowledge),
            "summary_available": int(bool(self._clean(data.get("summary")))),
        }

    def retrieve(
        self,
        project_id: int,
        query: str = "",
        max_events: int = 25,
    ) -> str:
        """Build relevant context for a new scene from long-term story memory."""
        project = self.db.get_project(project_id)
        if not project:
            raise ValueError(f"Project {project_id} does not exist.")

        events = self.db.list_events(project_id, limit=5000)
        characters = self.db.list_characters(project_id)
        open_threads = self.db.list_threads(project_id, status="open")
        paused_threads = self.db.list_threads(project_id, status="paused")
        chapters = self.db.list_chapters(project_id)

        terms = self._terms(query)

        def rank(event):
            searchable = " ".join([
                event.get("kind", ""),
                event.get("detail", ""),
                event.get("cause", ""),
                event.get("consequences", ""),
                " ".join(event.get("people", [])),
            ]).lower()

            overlap = sum(term in searchable for term in terms)
            importance = int(event.get("importance", 3))
            unresolved = bool(event.get("unresolved", False))
            return (
                overlap * 5
                + importance
                + (5 if unresolved else 0)
            )

        # Include query-relevant older facts, high-impact events,
        # unresolved consequences, and the most recent events.
        ranked = sorted(events, key=rank, reverse=True)
        selected_ids = {
            e["id"] for e in ranked[:max(1, max_events)]
        }

        recent = events[-min(8, len(events)):]
        selected_ids.update(e["id"] for e in recent)

        selected = [e for e in events if e["id"] in selected_ids]

        event_lines = []
        for e in selected:
            people = e.get("people", [])
            event_lines.append(
                f"- Ch.{e['chapter_no']} [{e['kind']}] {e['detail']}\n"
                f"  Cause: {e.get('cause') or 'Not established'}\n"
                f"  Consequences: "
                f"{e.get('consequences') or 'Not established'}\n"
                f"  People: {', '.join(people) if people else 'Not specified'}\n"
                f"  Unresolved consequence: "
                f"{'YES' if e.get('unresolved') else 'No'}"
            )

        character_lines = [
            f"- {c['name']}: {c['current_state'] or 'Current state not recorded'}"
            + (
                f" | Profile: {c['profile']}"
                if c.get("profile") else ""
            )
            for c in characters
        ]

        thread_lines = [
            f"- {t['title']}: {t['description']} "
            f"(introduced Ch.{t['introduced_chapter']})"
            for t in open_threads + paused_threads
        ]

        recent_chapters = [
            c for c in chapters if c["status"] == "complete"
        ][-3:]

        chapter_lines = [
            f"- Chapter {c['chapter_no']}: {c['title']}\n"
            f"  Summary: {c['summary'] or 'Summary not recorded'}"
            for c in recent_chapters
        ]

        return (
            f"NOVEL: {project['title']}\n"
            f"GENRE: {project['genre']}\n"
            f"PREMISE: {project['premise']}\n\n"
            f"CANON / STORY BIBLE:\n"
            f"{project['story_bible'] or 'Not yet established'}\n\n"
            f"RELEVANT HISTORICAL EVENTS:\n"
            f"{chr(10).join(event_lines) or 'No events recorded'}\n\n"
            f"CHARACTER STATES:\n"
            f"{chr(10).join(character_lines) or 'No character profiles recorded'}\n\n"
            f"OPEN OR PAUSED STORY THREADS:\n"
            f"{chr(10).join(thread_lines) or 'No unresolved threads recorded'}\n\n"
            f"RECENT COMPLETED CHAPTERS:\n"
            f"{chr(10).join(chapter_lines) or 'No completed chapters'}\n\n"
            "CONTINUITY RULE: Preserve established facts. Distinguish what "
            "the reader knows from what each character knows. Carry forward "
            "consequences, emotional changes, promises, secrets, and unresolved "
            "threads when relevant. Do not force every old detail into every scene."
        )

    def mark_thread_resolved(
        self,
        project_id: int,
        title: str,
        chapter_no: int,
    ) -> None:
        """Explicitly close a thread when the story resolves it."""
        matches = [
            t for t in self.db.list_threads(project_id)
            if t["title"].casefold() == title.casefold()
        ]
        if not matches:
            raise ValueError(f"Story thread not found: {title}")

        thread = matches[0]
        self.db.upsert_thread(
            project_id=project_id,
            title=thread["title"],
            description=thread["description"],
            status="resolved",
            introduced_chapter=thread["introduced_chapter"],
            resolved_chapter=chapter_no,
      )
          
