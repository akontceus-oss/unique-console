"""Провайдеры нейросетей: единый интерфейс для GPT, Gemini, Claude, YandexGPT и других.

Платформа общается с моделью только через LLMProvider.complete(system, user) → текст.
Чтобы подключить новую модель, достаточно одного класса с декоратором @register:

    @register
    class MyProvider(LLMProvider):
        id, title = "my", "Моя модель"
        def configured(self): return bool(self.s.my_api_key)
        def _complete(self, system, user, json_mode, max_tokens): ...

Ключи и модели задаются в .env. Какой провайдер использовать по умолчанию — AI_PROVIDER
(отчёт) и DIRECT_AI_PROVIDER (вкладка «Директ»); во вкладке его можно сменить для каждого запроса.

Модели передаются только обезличенные цифры: расходы, клики, площадки, запросы, кампании.
Выбирая провайдера, проверьте, что ваша страна и компания входят в список, где он работает.
"""
from __future__ import annotations

import json
import re

import httpx

from ..config import Settings, get_settings


class LLMError(RuntimeError):
    """Модель недоступна или ответила не так, как ожидалось (показывается человеку)."""


class LLMProvider:
    id = "base"
    title = "—"

    def __init__(self, settings: Settings | None = None, http: httpx.Client | None = None):
        self.s = settings or get_settings()
        self.http = http or httpx.Client(timeout=120)

    @property
    def model(self) -> str:
        return ""

    def configured(self) -> bool:
        return False

    def complete(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 2000) -> str:
        if not self.configured():
            raise LLMError(f"{self.title}: не заданы ключи в .env")
        try:
            text = self._complete(system, user, json_mode, max_tokens)
        except httpx.HTTPStatusError as e:
            raise LLMError(f"{self.title}: HTTP {e.response.status_code} {e.response.text[:300]}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"{self.title}: {type(e).__name__}: {e}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise LLMError(f"{self.title}: неожиданный ответ ({type(e).__name__})") from e
        if not text or not text.strip():
            raise LLMError(f"{self.title}: пустой ответ")
        return text.strip()

    def complete_json(self, system: str, user: str, max_tokens: int = 3000):
        return parse_json(self.complete(system, user, json_mode=True, max_tokens=max_tokens))

    def _complete(self, system: str, user: str, json_mode: bool, max_tokens: int) -> str:
        raise NotImplementedError

    # ------------------------------------------------------ вызов функций ---
    # Диалог агента хранится в общем виде, каждый провайдер переводит его в свой формат:
    #   {"role": "user", "content": "…"}
    #   {"role": "assistant", "content": "…", "tool_calls": [{"id", "name", "args"}]}
    #   {"role": "tool", "tool_call_id": "…", "name": "…", "content": "JSON-результат"}
    # tools: [{"name", "description", "parameters": JSON Schema}].
    native_tools = False

    def chat(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 4000) -> dict:
        """→ {"content": текст, "tool_calls": [{"id", "name", "args"}]}."""
        if not self.configured():
            raise LLMError(f"{self.title}: не заданы ключи в .env")
        try:
            if self.native_tools:
                out = self._chat(system, messages, tools, max_tokens)
            else:
                out = self._chat_json(system, messages, tools, max_tokens)
        except httpx.HTTPStatusError as e:
            raise LLMError(f"{self.title}: HTTP {e.response.status_code} {e.response.text[:300]}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"{self.title}: {type(e).__name__}: {e}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise LLMError(f"{self.title}: неожиданный ответ ({type(e).__name__})") from e
        out.setdefault("content", "")
        out.setdefault("tool_calls", [])
        for i, c in enumerate(out["tool_calls"]):
            c["id"] = c.get("id") or f"call_{len(messages)}_{i}"
            if isinstance(c.get("args"), str):
                c["args"] = json.loads(c["args"] or "{}")
            c["args"] = c.get("args") or {}
        return out

    def _chat(self, system, messages, tools, max_tokens) -> dict:
        raise NotImplementedError

    def _chat_json(self, system, messages, tools, max_tokens) -> dict:
        """Для моделей без встроенного вызова функций: протокол через JSON в тексте."""
        spec = json.dumps([{k: t[k] for k in ("name", "description", "parameters")} for t in tools], ensure_ascii=False)
        sys2 = (system + "\n\nДоступные функции (JSON Schema):\n" + spec + "\n\nОтвечай ТОЛЬКО JSON-объектом: "
                '{"tool_calls": [{"name": "…", "args": {…}}]} — чтобы вызвать функции, или {"content": "ответ"} — когда закончил.')
        lines = []
        for m in messages:
            if m["role"] == "user":
                lines.append("Пользователь: " + m["content"])
            elif m["role"] == "assistant":
                calls = m.get("tool_calls") or []
                lines.append("Ты: " + (json.dumps({"tool_calls": [{"name": c["name"], "args": c["args"]} for c in calls]}, ensure_ascii=False)
                                       if calls else m.get("content", "")))
            else:
                lines.append(f"Результат {m['name']}: {m['content']}")
        data = parse_json(self.complete(sys2, "\n\n".join(lines), json_mode=True, max_tokens=max_tokens))
        if not isinstance(data, dict):
            raise ValueError("ожидался объект")
        return {"content": str(data.get("content") or ""), "tool_calls": [
            {"name": c.get("name"), "args": c.get("args") or {}} for c in data.get("tool_calls") or [] if isinstance(c, dict)]}

    def describe(self) -> dict:
        return {"id": self.id, "title": self.title, "model": self.model, "configured": self.configured()}


PROVIDERS: dict[str, type[LLMProvider]] = {}


def register(cls: type[LLMProvider]) -> type[LLMProvider]:
    PROVIDERS[cls.id] = cls
    return cls


def get_provider(name: str | None = None, http: httpx.Client | None = None) -> LLMProvider:
    s = get_settings()
    name = (name or s.direct_ai_provider or s.ai_provider or "template").lower()
    if name not in PROVIDERS:
        raise LLMError(f"Неизвестный провайдер ИИ «{name}». Доступны: {', '.join(PROVIDERS)}")
    return PROVIDERS[name](s, http)


def list_providers() -> list[dict]:
    s = get_settings()
    return [cls(s).describe() for cls in PROVIDERS.values() if cls.id != "template"]


def parse_json(text: str):
    """JSON из ответа модели: модели иногда оборачивают его в ```json … ``` или добавляют слова вокруг."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except ValueError:
        start = min((i for i in (t.find("{"), t.find("[")) if i >= 0), default=-1)
        end = max(t.rfind("}"), t.rfind("]"))
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except ValueError:
                pass
    raise LLMError("Модель вернула не JSON")


# ================================================================ провайдеры ===
@register
class TemplateProvider(LLMProvider):
    """Без ИИ: платформа работает только по правилам."""

    id, title = "template", "Без ИИ"

    def configured(self) -> bool:
        return False


@register
class OpenAIProvider(LLMProvider):
    """GPT через OpenAI API. OPENAI_BASE_URL позволяет подключить любой совместимый сервис
    (OpenRouter, DeepSeek, Azure OpenAI, локальную модель) без нового кода."""

    id, title = "openai", "GPT (OpenAI)"

    @property
    def model(self) -> str:
        return self.s.openai_model

    def configured(self) -> bool:
        return bool(self.s.openai_api_key)

    def _complete(self, system, user, json_mode, max_tokens):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = self.http.post(
            self.s.openai_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {self.s.openai_api_key}", "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    native_tools = True

    def _chat(self, system, messages, tools, max_tokens):
        msgs = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                msgs.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                    for c in m["tool_calls"]]})
            elif m["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                msgs.append({"role": m["role"], "content": m.get("content") or ""})
        r = self.http.post(
            self.s.openai_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {self.s.openai_api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "messages": msgs, "max_completion_tokens": max_tokens,
                  "tools": [{"type": "function", "function": t} for t in tools]},
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        return {"content": msg.get("content") or "", "tool_calls": [
            {"id": c["id"], "name": c["function"]["name"], "args": c["function"].get("arguments") or "{}"}
            for c in msg.get("tool_calls") or []]}


@register
class GeminiProvider(LLMProvider):
    id, title = "gemini", "Gemini (Google)"

    @property
    def model(self) -> str:
        return self.s.gemini_model

    def configured(self) -> bool:
        return bool(self.s.gemini_api_key)

    def _complete(self, system, user, json_mode, max_tokens):
        cfg = {"maxOutputTokens": max_tokens, "temperature": 0.2}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        r = self.http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.s.gemini_api_key, "Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": cfg,
            },
        )
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    native_tools = True

    def _chat(self, system, messages, tools, max_tokens):
        contents = []
        for m in messages:
            if m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                # Части ответа возвращаются как есть: в них подпись рассуждений (thoughtSignature),
                # без которой новые модели Gemini не принимают продолжение вызова функций.
                parts = (m.get("raw") or {}).get("gemini")
                if not parts:
                    parts = [{"text": m["content"]}] if m.get("content") else []
                    parts += [{"functionCall": {"name": c["name"], "args": c["args"]}} for c in m.get("tool_calls") or []]
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:
                part = {"functionResponse": {"name": m["name"], "response": {"result": json.loads(m["content"])}}}
                if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].append(part)  # ответы на несколько вызовов — одним сообщением
                else:
                    contents.append({"role": "user", "parts": [part]})
        r = self.http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.s.gemini_api_key, "Content-Type": "application/json"},
            json={"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                  "tools": [{"functionDeclarations": tools}], "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2}},
        )
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"].get("parts", [])
        return {"content": "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought")),
                "tool_calls": [{"name": p["functionCall"]["name"], "args": p["functionCall"].get("args") or {}}
                               for p in parts if "functionCall" in p],
                "raw": {"gemini": parts}}


@register
class AnthropicProvider(LLMProvider):
    id, title = "anthropic", "Claude (Anthropic)"

    @property
    def model(self) -> str:
        return self.s.anthropic_model

    def configured(self) -> bool:
        return bool(self.s.anthropic_api_key)

    def _complete(self, system, user, json_mode, max_tokens):
        if json_mode:
            system += "\nОтветь только JSON, без пояснений."
        r = self.http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]},
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []))

    native_tools = True

    def _chat(self, system, messages, tools, max_tokens):
        msgs = []
        for m in messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]} for c in m.get("tool_calls") or []]
                msgs.append({"role": "assistant", "content": blocks})
            else:
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
                    msgs[-1]["content"].append(block)
                else:
                    msgs.append({"role": "user", "content": [block]})
        r = self.http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": max_tokens, "system": system, "messages": msgs,
                  "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]},
        )
        r.raise_for_status()
        content = r.json().get("content", [])
        return {"content": "".join(b.get("text", "") for b in content if b.get("type") == "text"),
                "tool_calls": [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}} for b in content if b.get("type") == "tool_use"]}


@register
class YandexGPTProvider(LLMProvider):
    id, title = "yandexgpt", "YandexGPT"

    @property
    def model(self) -> str:
        return self.s.yandexgpt_model

    def configured(self) -> bool:
        return bool(self.s.yandexgpt_api_key and self.s.yandexgpt_folder_id)

    def _complete(self, system, user, json_mode, max_tokens):
        body = {
            "modelUri": f"gpt://{self.s.yandexgpt_folder_id}/{self.model}",
            "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": max_tokens},
            "messages": [{"role": "system", "text": system}, {"role": "user", "text": user}],
        }
        if json_mode:
            body["jsonObject"] = True
        r = self.http.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={"Authorization": f"Api-Key {self.s.yandexgpt_api_key}", "x-folder-id": self.s.yandexgpt_folder_id},
            json=body,
        )
        r.raise_for_status()
        return r.json()["result"]["alternatives"][0]["message"]["text"]
