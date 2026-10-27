from __future__ import annotations

import json
import os
import urllib.request

from aiops.config import Settings


class LLMError(Exception):
    pass


def load_skill(skills_dir: str, name: str) -> str:
    path = os.path.join(skills_dir, name, "SKILL.md")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                text = "\n".join(lines[index + 1:])
                break

    return text.strip()


def chat_json(settings: Settings, skill: str, payload: dict) -> dict:
    if not settings.llm_url:
        raise LLMError("LLM URL is not configured")

    skill_text = load_skill(settings.skills_dir, skill)
    user_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    messages = [
        {"role": "system", "content": skill_text},
        {"role": "user", "content": user_content},
    ]
    body = {
        "model": settings.llm_model,
        "temperature": 0.2,
        "max_tokens": settings.llm_max_tokens,
        "messages": messages,
    }

    last_error = "no response"
    for attempt in range(2):
        data = _post(settings, body)
        content = _extract_content(data)
        try:
            return _parse_json(content)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            if attempt == 0:
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": f"Invalid JSON: {last_error}. Reply with the JSON object only.",
                    }
                )
                body["messages"] = messages

    raise LLMError(f"LLM response is not valid JSON: {last_error}")


def _post(settings: Settings, body: dict) -> dict:
    url = settings.llm_url.rstrip("/") + "/chat/completions"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = response.read().decode("utf-8")
    except Exception as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM response is not JSON: {exc}") from exc


def _extract_content(data: dict) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _parse_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found")

    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("JSON root is not an object")
    return parsed
