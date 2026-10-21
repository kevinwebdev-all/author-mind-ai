
"""AuthorMind writing engine: planning, scene generation, and recovery."""
from __future__ import annotations

import json
import re
from typing import Any

from db import Database
from memory import StoryMemory
from providers import ModelProvider


AUTHOR_SYSTEM = """You are AuthorMind, a professional novelist and story architect.

Write original, specific, emotionally convincing fiction with deliberate
pacing, subtext, distinctive character voices, vivid but controlled prose,
credible motivations, and meaningful consequences.

Continuity is mandatory:
- Preserve established canon and the supplied story bible.
- Distinguish facts from rumours, beliefs, secrets, and lies.
- Characters must not know information they have not learned.
- Choices must have believable consequences.
- Do not repeat previous scenes or restart the novel.
- Do not resolve major conflicts prematurely.
- Avoid generic filler, artificial dialogue, repetitive descriptions,
  melodrama without justification, and rushed endings.
- Follow the chapter and scene plan.
- Return only the requested output format."""


class EngineError(RuntimeError):
    """An actionable writing-engine error."""


class NovelEngine:
    def __init__(
        self,
        database: Database,
        provider: ModelProvider,
        memory: StoryMemory,
    ):
        self.db = database
        self.ai = provider
        self.memory = memory

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
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
                raise EngineError("The model returned invalid JSON.")
            try:
                value = json.loads(text[start:end + 1])
            except json.JSONDecodeError as exc:
                raise EngineError(
                    "The model returned incomplete JSON. Retry the operation."
                ) from exc

        if not isinstance(value, dict):
            raise EngineError("The model response must be a JSON object.")
        return value

    @staticmethod
    def _text(value, limit=12000):
        if not isinstance(value, str):
            return ""
        return value.strip()[:limit]

    def create_plan(self, project_id: int) -> dict:
        """Generate and persist a full novel plan before drafting."""
        project = self.db.get_project(project_id)
        if not project:
            raise EngineError("Novel project not found.")

        if project["status"] == "complete":
            raise EngineError(
                "This novel is marked complete. Create a new project to begin another."
            )

        prompt = f"""Design a complete, coherent novel plan.

Return ONLY valid JSON in this shape:
{{
  "story_bible": "Detailed canon and writing guide as text",
  "chapters": [
    {{
      "number": 1,
      "title": "Chapter title",
      "outline": "Chapter purpose, conflict, development, and outcome",
      "scenes": [
        {{
          "goal": "What the scene must accomplish",
          "conflict": "Specific obstacle or tension",
          "emotional_turn": "How the emotional situation changes",
          "consequence": "What changes because of this scene"
        }}
      ]
    }}
  ]
}}

Requirements:
- Plan 18 chapters.
- Each chapter has exactly 3 distinct scene briefs.
- Each scene brief must be concrete enough to write without inventing
  the entire plot during drafting.
- Include a strong opening, escalating conflict, reversals, subplots,
  character growth, meaningful consequences, and an earned ending.
- Establish character goals, fears, contradictions, relationships,
  voices, and knowledge boundaries.
- Track timeline rules, secrets, promises, objects, and unresolved threads.
- Make every chapter advance the story.
- Avoid repetitive conflicts and artificial misunderstandings.
- Preserve the user's premise; do not replace it with a different story.

Title: {project['title']}
Genre: {project['genre']}
Premise: {project['premise']}
"""

        raw = self.ai.complete(
            [
                {"role": "system", "content": AUTHOR_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=7000,
            temperature=0.55,
        )
        data = self._parse_json(raw)

        bible = self._text(data.get("story_bible"), 30000)
        chapters = data.get("chapters")

        if len(bible) < 100:
            raise EngineError(
                "The story bible is incomplete. The plan was not saved."
            )

        if not isinstance(chapters, list) or len(chapters) < 12:
            raise EngineError(
                "The model returned too few chapters. The plan was not saved."
            )

        cleaned = []
        seen_numbers = set()

        for item in chapters:
            if not isinstance(item, dict):
                continue

            try:
                number = int(item["number"])
            except (KeyError, TypeError, ValueError):
                continue

            if number in seen_numbers or number < 1:
                continue

            title = self._text(item.get("title"), 200)
            outline = self._text(item.get("outline"), 4000)
            scenes = item.get("scenes")

            if (
                not title
                or len(outline) < 30
                or not isinstance(scenes, list)
                or len(scenes) != 3
            ):
                continue

            valid_scenes = []
            for scene in scenes:
                if not isinstance(scene, dict):
                    break

                required = [
                    self._text(scene.get(key), 1000)
                    for key in (
                        "goal",
                        "conflict",
                        "emotional_turn",
                        "consequence",
                    )
                ]

                if not all(required):
                    break

                valid_scenes.append({
                    "goal": required[0],
                    "conflict": required[1],
                    "emotional_turn": required[2],
                    "consequence": required[3],
                })

            if len(valid_scenes) != 3:
                continue

            cleaned.append({
                "number": number,
                "title": title,
                "outline": outline,
                "scenes": valid_scenes,
            })
            seen_numbers.add(number)

        cleaned.sort(key=lambda x: x["number"])

        if (
            len(cleaned) < 12
            or [x["number"] for x in cleaned]
            != list(range(1, len(cleaned) + 1))
        ):
            raise EngineError(
                "The generated plan failed validation. Nothing was saved; "
                "retry /plan."
            )

        # Do not overwrite a plan for a novel already being drafted.
        existing = self.db.get_plan(project_id)
        chapters = self.db.list_chapters(project_id)

        if existing and any(
            c["content"].strip() for c in chapters
        ):
            raise EngineError(
                "This novel already has drafted content. "
                "Its plan was protected from accidental replacement."
            )

        self.db.save_plan(project_id, bible, cleaned)
        self.db.set_project_status(project_id, "writing")

        return {
            "project_id": project_id,
            "title": project["title"],
            "chapter_count": len(cleaned),
            "scenes_per_chapter": 3,
            "status": "planned",
        }

    def _finalize_chapter(self, project, chapter, plan_item):
        """Retry memory extraction without writing another scene."""
        content = chapter["content"].strip()
        if not content:
            raise EngineError("Cannot finalize an empty chapter.")

        # The chapter content is already checkpointed. If this fails,
        # the chapter remains unfinished and the next call retries it.
        self.memory.record_chapter(
            project["id"],
            chapter["chapter_no"],
            content,
        )

        summary_prompt = f"""Write a factual chapter summary in 100-180 words.
Include major actions, decisions, relationship changes, revelations,
and consequences. Do not invent events. Return summary text only.

Chapter: {plan_item['title']}
Chapter plan: {plan_item['outline']}

Manuscript:
{content[-12000:]}
"""
        summary = self.ai.complete(
            [
                {
                    "role": "system",
                    "content": "Summarize fiction accurately and concisely.",
                },
                {"role": "user", "content": summary_prompt},
            ],
            max_tokens=350,
            temperature=0.1,
        )

        if len(summary.strip()) < 40:
            raise EngineError(
                "The chapter summary was incomplete. Finalization can be retried."
            )

        self.db.complete_chapter(
            project["id"],
            chapter["chapter_no"],
            summary.strip(),
        )

        return {
            "chapter": chapter["chapter_no"],
            "title": chapter["title"],
            "status": "complete",
        }

    def continue_novel(self, project_id: int) -> dict:
        """Write one scene, or safely retry chapter finalization."""
        project = self.db.get_project(project_id)
        if not project:
            raise EngineError("Novel project not found.")

        plan = self.db.get_plan(project_id)
        if not plan:
            raise EngineError("No plan exists. Run /plan first.")

        chapters = self.db.list_chapters(project_id)
        chapter = next(
            (c for c in chapters if c["status"] != "complete"),
            None,
        )

        if chapter is None:
            self.db.set_project_status(project_id, "complete")
            return {
                "status": "complete",
                "message": "All planned chapters are complete.",
            }

        plan_item = next(
            (
                item for item in plan
                if item["number"] == chapter["chapter_no"]
            ),
            None,
        )
        if plan_item is None:
            raise EngineError(
                f"Missing plan for chapter {chapter['chapter_no']}."
            )

        scenes = plan_item["scenes"]
        scene_index = int(chapter["scene_index"])

        # A scene may already have been saved when memory/API finalization
        # failed. Retry finalization; never append the same scene again.
        if scene_index >= len(scenes):
            result = self._finalize_chapter(
                project, chapter, plan_item
            )
            result["recovered_checkpoint"] = True
            return result

        scene_plan = scenes[scene_index]

        query = " ".join([
            plan_item["outline"],
            scene_plan["goal"],
            scene_plan["conflict"],
            scene_plan["emotional_turn"],
            scene_plan["consequence"],
        ])
        context = self.memory.retrieve(project_id, query=query)

        previous_text = chapter["content"].strip()
        previous_text = previous_text[-9000:] if previous_text else (
            "(No scenes written in this chapter yet.)"
        )

        prompt = f"""Write exactly ONE scene for Chapter {chapter['chapter_no']}:
{plan_item['title']}

SCENE: {scene_index + 1} of {len(scenes)}

CHAPTER OUTLINE:
{plan_item['outline']}

THIS SCENE'S BRIEF:
Goal: {scene_plan['goal']}
Conflict: {scene_plan['conflict']}
Emotional turn: {scene_plan['emotional_turn']}
Consequence: {scene_plan['consequence']}

LONG-TERM STORY MEMORY:
{context}

EXISTING TEXT OF THIS CHAPTER:
{previous_text}

Writing requirements:
- Continue from the existing chapter; never restart or recap it.
- Write approximately 600-900 words of finished prose.
- Begin where the story naturally continues.
- Use specific action, subtext, credible dialogue, and meaningful detail.
- Follow the scene brief and make its consequences visible.
- Do not write the next scene or chapter.
- Do not add a chapter heading.
- Do not describe your process or provide commentary.
- Return manuscript prose only.
"""

        text = self.ai.complete(
            [
                {"role": "system", "content": AUTHOR_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1800,
            temperature=0.8,
        )

        if len(text.strip()) < 500:
            raise EngineError(
                "The scene response was too short to accept. "
                "No scene checkpoint was advanced; retry /continue."
            )

        # Save the scene and advance its checkpoint together.
        # If the model call failed, this block never runs.
        self.db.save_scene(
            project_id=project_id,
            chapter_no=chapter["chapter_no"],
            scene_text=text.strip(),
            scene_index=scene_index + 1,
            title=plan_item["title"],
        )

        if scene_index + 1 < len(scenes):
            return {
                "status": "scene_saved",
                "chapter": chapter["chapter_no"],
                "chapter_title": plan_item["title"],
                "scene": scene_index + 1,
                "scenes_remaining": len(scenes) - scene_index - 1,
                "words_approx": len(text.split()),
                "message": "Scene saved. Send /continue for the next scene.",
            }

        # All planned scenes are saved. Finalize memory and summary.
        updated_chapter = self.db.get_chapter(
            project_id, chapter["chapter_no"]
        )
        result = self._finalize_chapter(
            project, updated_chapter, plan_item
        )
        result["status"] = "chapter_complete"
        result["scenes_written"] = len(scenes)
        return result

    def get_status(self, project_id: int) -> dict:
        project = self.db.get_project(project_id)
        if not project:
            raise EngineError("Novel project not found.")

        chapters = self.db.list_chapters(project_id)
        complete = sum(
            c["status"] == "complete" for c in chapters
        )
        active = next(
            (c for c in chapters if c["status"] != "complete"),
            None,
        )

        word_count = sum(
            len(re.findall(
                r"\b[\w'-]+\b", c["content"]
            ))
            for c in chapters
        )

        return {
            "project_id": project_id,
            "title": project["title"],
            "status": project["status"],
            "planned_chapters": len(chapters),
            "completed_chapters": complete,
            "current_chapter": (
                active["chapter_no"] if active else None
            ),
            "saved_scene_index": (
                active["scene_index"] if active else None
            ),
            "approximate_words": word_count,
        }
