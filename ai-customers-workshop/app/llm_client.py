"""OpenRouter LLM client via the OpenAI SDK."""

import json
import logging
from openai import AsyncOpenAI

from app.config import OPENROUTER_API_KEY

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)


def get_client() -> AsyncOpenAI:
    return _client


async def call_llm(
    messages: list[dict],
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    json_mode: bool = False,
) -> dict:
    """Call the LLM and return parsed JSON or raw text.

    If json_mode=True, tries response_format=json_object first,
    then falls back to parsing JSON from the response text.
    """
    client = get_client()

    kwargs = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    if json_mode:
        try:
            kwargs["response_format"] = {"type": "json_object"}
            resp = await client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            return {"parsed": json.loads(content), "raw": content, "usage": _usage(resp)}
        except Exception as e:
            logger.warning("json_object mode failed (%s), falling back to prompt-based", e)
            kwargs.pop("response_format", None)

    resp = await client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""

    if json_mode:
        parsed = _extract_json(content)
        return {"parsed": parsed, "raw": content, "usage": _usage(resp)}

    return {"text": content, "usage": _usage(resp)}


def _extract_json(text: str) -> dict:
    """Extract JSON from text, handling markdown code blocks."""
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from ```json ... ``` block
    if "```json" in text:
        block = text.split("```json", 1)[1].split("```", 1)[0]
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            pass

    # Try extracting from ``` ... ``` block
    if "```" in text:
        block = text.split("```", 1)[1].split("```", 1)[0]
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            pass

    # Try finding first { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {"error": "Could not parse JSON from response", "raw_text": text}


def _usage(resp) -> dict:
    if resp.usage:
        return {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
        }
    return {}
