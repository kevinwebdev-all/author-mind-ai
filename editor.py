
import json
from typing import Any

from db import Database
from providers import ModelProvider
from memory import StoryMemory


class NovelEditor:
    """Reviews chapters for quality and continuity without changing them."""

    def __init__(
        self,
        database: Database,
        provider: ModelProvider,
        memory: StoryMemory,
    ):
        self.db = database
        self.provider = provider
        self.memory = memory

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        text = text.strip()

        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        start = text.find("{")
        end = text.rfind("}")

        if start < 0 or end <= start:
            raise ValueError("The AI returned no valid JSON object.")

        result = json.loads(text[start:end + 1])

        if not isinstance(result, dict):
            raise ValueError("The review must be a JSON object.")

        return result

    def review_chapter(
        self,
        project_id: int,
        chapter_no: int,
    ) -> dict[str, Any]:
        project = self.db.get_project(project_id)

        if not project:
            raise ValueError("Novel project not found.")

        chapter = self.db.get_chapter(project_id, chapter_no)

        if not chapter:
            raise ValueError(
                f"Chapter {chapter_no} has not been written yet."
            )

        content = chapter.get("content", "").strip()

        if len(content) < 100:
            raise ValueError("This chapter is too short to review.")

        context = self.memory.retrieve(
            project_id,
            query=f"Review chapter {chapter_no} for continuity",
            max_events=35,
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict professional fiction editor. "
                    "Review the supplied chapter without rewriting it. "
                    "Check continuity, character motivation, timeline, "
                    "cause and effect, pacing, dialogue, repetition, "
                    "emotional depth, prose quality, and unresolved plot "
                    "threads. Do not invent errors unsupported by the text. "
                    "Distinguish definite contradictions from possible "
                    "concerns. Return JSON only, using this structure: "
                    '{"overall_score": 1, "strengths": [], '
                    '"issues": [{"severity": "high|medium|low", '
                    '"category": "", "evidence": "", '
                    '"recommendation": ""}], '
                    '"continuity_check": "", "next_step": ""}. '
                    "Scores must be integers from 1 to 10. "
                    "Keep findings specific and actionable."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Novel title: {project.get('title', 'Untitled')}\n"
                    f"Genre: {project.get('genre', 'Unspecified')}\n"
                    f"Chapter: {chapter_no}\n\n"
                    f"Established story context:\n{context}\n\n"
                    f"Chapter text:\n{content}"
                ),
            },
        ]

        response = self.provider.complete(
            messages,
            max_tokens=2500,
            temperature=0.2,
        )

        result = self._parse_json(response)

        score = result.get("overall_score")
        if not isinstance(score, int) or isinstance(score, bool):
            result["overall_score"] = 0
        else:
            result["overall_score"] = max(1, min(10, score))

        for key in ("strengths", "issues"):
            if not isinstance(result.get(key), list):
                result[key] = []

        if not isinstance(result.get("continuity_check"), str):
            result["continuity_check"] = ""

        if not isinstance(result.get("next_step"), str):
            result["next_step"] = ""

        return result
      
