
"""AuthorMind model providers with bounded retries and failover."""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any

import requests


class ProviderError(RuntimeError):
    """Raised when no configured model provider can complete a request."""


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    model: str
    base_url: str


class ModelProvider:
    """OpenAI-compatible and Gemini provider adapter with failover."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.timeout = self._bounded_int("AI_TIMEOUT_SECONDS", 90, 10, 180)
        self.max_retries = self._bounded_int("AI_MAX_RETRIES", 2, 0, 4)
        self.providers = self._load_providers()

        if not self.providers:
            raise ProviderError(
                "No AI provider configured. Set OPENROUTER_API_KEY, "
                "GROK_API_KEY, or GEMINI_API_KEY."
            )

    @staticmethod
    def _bounded_int(name, default, low, high):
        try:
            return max(low, min(high, int(os.getenv(name, str(default)))))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _load_providers():
        configs = []

        openrouter = os.getenv("OPENROUTER_API_KEY", "").strip()
        grok = (
            os.getenv("GROK_API_KEY")
            or os.getenv("XAI_API_KEY")
            or ""
        ).strip()
        gemini = os.getenv("GEMINI_API_KEY", "").strip()

        if openrouter:
            configs.append(ProviderConfig(
                "openrouter",
                openrouter,
                os.getenv(
                    "OPENROUTER_MODEL", "openai/gpt-4o-mini"
                ).strip(),
                "https://openrouter.ai/api/v1/chat/completions",
            ))

        if grok:
            configs.append(ProviderConfig(
                "grok",
                grok,
                os.getenv("GROK_MODEL", "grok-3-mini").strip(),
                "https://api.x.ai/v1/chat/completions",
            ))

        if gemini:
            configs.append(ProviderConfig(
                "gemini",
                gemini,
                os.getenv(
                    "GEMINI_MODEL", "gemini-2.5-flash"
                ).strip(),
                "https://generativelanguage.googleapis.com/v1beta",
            ))

        return configs

    @staticmethod
    def _validate_messages(messages):
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list.")

        for item in messages:
            if not isinstance(item, dict):
                raise ValueError("Each message must be an object.")

            if item.get("role") not in {
                "system", "user", "assistant"
            }:
                raise ValueError(
                    "Message role must be system, user, or assistant."
                )

            if (
                not isinstance(item.get("content"), str)
                or not item["content"].strip()
            ):
                raise ValueError(
                    "Every message needs non-empty text content."
                )

    @staticmethod
    def _error_message(response):
        try:
            body: Any = response.json()
            message = (
                body.get("error", body)
                if isinstance(body, dict)
                else body
            )
            if isinstance(message, dict):
                message = message.get(
                    "message", message.get("detail", message)
                )
            return str(message)[:500]
        except ValueError:
            return response.text[:500]

    @staticmethod
    def _is_retryable(status):
        return status in {408, 409, 425, 429, 500, 502, 503, 504}

    def _post_with_retries(self, url, headers, payload, provider_name):
        last_error = "unknown error"

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )

                if response.status_code >= 400:
                    last_error = (
                        f"HTTP {response.status_code}: "
                        f"{self._error_message(response)}"
                    )

                    if not self._is_retryable(response.status_code):
                        raise ProviderError(
                            f"{provider_name}: {last_error}"
                        )

                    if attempt < self.max_retries:
                        time.sleep(
                            min(2 ** attempt + random.random(), 6)
                        )
                        continue
                    break

                try:
                    data = response.json()
                except ValueError as exc:
                    raise ProviderError(
                        f"{provider_name}: response was not valid JSON"
                    ) from exc

                if not isinstance(data, dict):
                    raise ProviderError(
                        f"{provider_name}: unexpected response format"
                    )

                return data

            except ProviderError:
                raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"

                if attempt < self.max_retries:
                    time.sleep(
                        min(2 ** attempt + random.random(), 6)
                    )
                    continue
                break
            except requests.RequestException as exc:
                raise ProviderError(
                    f"{provider_name}: request failed: {exc}"
                ) from exc

        raise ProviderError(
            f"{provider_name}: retries exhausted ({last_error})"
        )

    def _openai_compatible(
        self, provider, messages, max_tokens, temperature
    ):
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }

        if provider.name == "openrouter":
            headers["X-Title"] = "AuthorMind"

        payload = {
            "model": provider.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        data = self._post_with_retries(
            provider.base_url, headers, payload, provider.name
        )

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"{provider.name}: missing message content"
            ) from exc

        if isinstance(content, list):
            content = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            )

        if not isinstance(content, str) or not content.strip():
            raise ProviderError(
                f"{provider.name}: model returned empty content"
            )

        return content.strip()

    def _gemini(self, provider, messages, max_tokens, temperature):
        system_parts = [
            m["content"] for m in messages if m["role"] == "system"
        ]
        contents = []

        for message in messages:
            if message["role"] == "system":
                continue

            role = (
                "model" if message["role"] == "assistant" else "user"
            )
            contents.append({
                "role": role,
                "parts": [{"text": message["content"]}],
            })

        if not contents:
            contents = [{
                "role": "user",
                "parts": [{"text": "Follow the system instructions."}],
            }]

        payload = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }

        if system_parts:
            payload["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(system_parts)}]
            }

        url = (
            f"{provider.base_url}/models/{provider.model}:generateContent"
        )
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": provider.api_key,
        }

        data = self._post_with_retries(
            url, headers, payload, provider.name
        )

        try:
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict)
            )
        except (KeyError, IndexError, TypeError) as exc:
            feedback = data.get("promptFeedback", {})
            reason = (
                feedback.get("blockReason", "unknown")
                if isinstance(feedback, dict)
                else "unknown"
            )
            raise ProviderError(
                f"gemini: no usable candidate ({reason})"
            ) from exc

        if not content.strip():
            raise ProviderError("gemini: model returned empty content")

        return content.strip()

    def complete(
        self, messages, max_tokens=1800, temperature=0.7
    ):
        """Generate text, falling back to the next configured provider."""
        self._validate_messages(messages)

        try:
            max_tokens = max(1, min(8192, int(max_tokens)))
            temperature = max(0.0, min(2.0, float(temperature)))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "max_tokens and temperature must be numeric."
            ) from exc

        failures = []

        for provider in self.providers:
            try:
                if provider.name == "gemini":
                    result = self._gemini(
                        provider, messages, max_tokens, temperature
                    )
                else:
                    result = self._openai_compatible(
                        provider, messages, max_tokens, temperature
                    )

                print(
                    f"AuthorMind: {provider.name} succeeded.",
                    flush=True,
                )
                return result

            except Exception as exc:
                failures.append(
                    f"{provider.name}: {str(exc)[:250]}"
                )
                print(
                    f"AuthorMind: {provider.name} failed: "
                    f"{str(exc)[:250]}",
                    flush=True,
                )

        raise ProviderError(
            "All configured providers failed. "
            + " | ".join(failures)
      )
      
