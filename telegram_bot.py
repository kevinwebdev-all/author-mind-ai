
import logging
import os
import tempfile
from pathlib import Path

import requests

from db import Database
from providers import ModelProvider
from memory import StoryMemory
from engine import NovelEngine
from editor import NovelEditor
from exports import NovelExporter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramNovelBot:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")

        self.api = f"{TELEGRAM_API}/bot{self.token}"
        self.db = Database()
        self.provider = ModelProvider()
        self.memory = StoryMemory(self.db, self.provider)
        self.engine = NovelEngine(self.db, self.provider, self.memory)
        self.editor = NovelEditor(self.db, self.provider, self.memory)
        self.exporter = NovelExporter()
        self.offset = None

    def _request(self, method, payload=None, timeout=60):
        response = requests.post(
            f"{self.api}/{method}",
            json=payload or {},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram API error"))
        return data.get("result")

    def send_text(self, chat_id, text):
        text = str(text).strip() or "No response."
        # Telegram text messages have a 4096-character limit.
        for start in range(0, len(text), 4000):
            self._request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text[start:start + 4000],
                },
            )

    def send_document(self, chat_id, filename, content):
        response = requests.post(
            f"{self.api}/sendDocument",
            data={"chat_id": chat_id},
            files={
                "document": (
                    filename,
                    content,
                    "application/octet-stream",
                )
            },
            timeout=120,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Upload failed"))

    @staticmethod
    def help_text():
        return (
            "AUTHOR MIND AI\n\n"
            "/new Title | Genre | Premise — create a novel\n"
            "/plan — create the story plan\n"
            "/continue — write the next scene\n"
            "/status — show writing progress\n"
            "/review 1 — review chapter 1\n"
            "/export txt — export TXT\n"
            "/export pdf — export PDF\n"
            "/export docx — export DOCX\n"
            "/novels — list your novels\n"
            "/use ID — select a novel\n"
            "/help — show commands\n\n"
            "After /new, run /plan, then /continue repeatedly."
        )

    def _project_id(self, chat_id):
        value = self.db.latest_project(chat_id)
        if not value:
            return None
        if isinstance(value, dict):
            return value.get("id") or value.get("project_id")
        return getattr(value, "id", None) or getattr(value, "project_id", None)

    def handle(self, chat_id, text):
        text = text.strip()
        if not text:
            return

        command, _, argument = text.partition(" ")
        command = command.lower()

        if command in ("/start", "/help"):
            self.send_text(chat_id, self.help_text())
            return

        if command == "/new":
            parts = [part.strip() for part in argument.split("|", 2)]
            if len(parts) != 3 or not all(parts):
                self.send_text(
                    chat_id,
                    "Use:\n/new Title | Genre | Premise",
                )
                return

            project_id = self.db.create_project(
                chat_id=chat_id,
                title=parts[0],
                genre=parts[1],
                premise=parts[2],
            )
            self.send_text(
                chat_id,
                f"Novel created. Project ID: {project_id}\n"
                "Next, send /plan.",
            )
            return

        if command == "/novels":
            projects = self.db.list_projects(chat_id)
            if not projects:
                self.send_text(chat_id, "No novels yet. Use /new.")
                return

            lines = ["Your novels:"]
            for project in projects:
                if isinstance(project, dict):
                    pid = project.get("id") or project.get("project_id")
                    title = project.get("title", "Untitled")
                else:
                    pid = getattr(project, "id", None)
                    title = getattr(project, "title", "Untitled")
                lines.append(f"{pid}: {title}")
            self.send_text(chat_id, "\n".join(lines))
            return

        if command == "/use":
            try:
                project_id = int(argument.strip())
            except ValueError:
                self.send_text(chat_id, "Use /use PROJECT_ID")
                return

            project = self.db.get_project(project_id)
            if not project:
                self.send_text(chat_id, "Novel not found.")
                return

            owner = project.get("chat_id") if isinstance(project, dict) else None
            if owner is not None and int(owner) != int(chat_id):
                self.send_text(chat_id, "That novel does not belong to you.")
                return

            self._request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        f"Selected: {project.get('title', 'Untitled')}"
                        if isinstance(project, dict)
                        else "Novel selected."
                    ),
                },
            )
            self.db.set_project_status(project_id, "active")
            self._selected_projects[chat_id] = project_id
            return

        project_id = self._selected_projects.get(chat_id)
        if project_id is None:
            project_id = self._project_id(chat_id)

        if not project_id:
            self.send_text(chat_id, "Create a novel first with /new.")
            return

        if command == "/plan":
            self.send_text(chat_id, "Creating the full story plan...")
            result = self.engine.create_plan(project_id)
            self.send_text(chat_id, str(result))
            return

        if command == "/continue":
            self.send_text(chat_id, "Writing the next scene...")
            result = self.engine.continue_novel(project_id)
            self.send_text(chat_id, str(result))
            return

        if command == "/status":
            result = self.engine.get_status(project_id)
            self.send_text(chat_id, str(result))
            return

        if command == "/review":
            try:
                chapter_no = int(argument.strip())
            except ValueError:
                self.send_text(chat_id, "Use /review CHAPTER_NUMBER")
                return

            self.send_text(chat_id, "Reviewing chapter...")
            result = self.editor.review_chapter(project_id, chapter_no)
            self.send_text(chat_id, str(result))
            return

        if command == "/export":
            fmt = argument.strip().lower()
            if fmt not in {"txt", "pdf", "docx"}:
                self.send_text(chat_id, "Use /export txt, /export pdf, or /export docx.")
                return

            project = self.db.get_project(project_id)
            chapters = self.db.list_chapters(project_id)
            title = project.get("title", "Novel") if isinstance(project, dict) else "Novel"

            if fmt == "txt":
                filename, content = self.exporter.export_txt(title, chapters)
            elif fmt == "pdf":
                filename, content = self.exporter.export_pdf(title, chapters)
            else:
                filename, content = self.exporter.export_docx(title, chapters)

            self.send_document(chat_id, filename, content)
            return

        self.send_text(chat_id, "Unknown command. Use /help.")

    def run(self):
        self._selected_projects = {}

        self._request("deleteWebhook", {"drop_pending_updates": False})
        self.send_text if False else None
        logger.info("AuthorMind Telegram bot started.")

        while True:
            try:
                payload = {"timeout": 25}
                if self.offset is not None:
                    payload["offset"] = self.offset

                updates = self._request("getUpdates", payload, timeout=35)

                for update in updates or []:
                    self.offset = update["update_id"] + 1
                    message = update.get("message", {})
                    chat = message.get("chat", {})
                    chat_id = chat.get("id")
                    text = message.get("text", "")

                    if chat_id is None or not text:
                        continue

                    try:
                        self.handle(chat_id, text)
                    except Exception:
                        logger.exception("Message handling failed")
                        try:
                            self.send_text(
                                chat_id,
                                "That action failed. Your saved progress may still be available. "
                                "Check Railway logs before retrying.",
                            )
                        except Exception:
                            logger.exception("Could not send error message")


if __name__ == "__main__":
    TelegramNovelBot().run()
      
