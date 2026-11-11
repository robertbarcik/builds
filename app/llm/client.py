"""OpenRouter client (openai SDK) with three call shapes, all metered by `Budget`:

- `json_call(role, messages)`: one JSON object back. Asks for `response_format=json_object`;
  if the provider rejects that, retries once with the prompt alone and parses loosely.
  Empty content counts as a failure.
- `stream_text(role, messages)`: real token streaming. Yields text deltas; reasoning /
  thinking parts (`delta.reasoning*` fields and inline `<think>` blocks) never reach the
  caller. Usage (asked for in the stream) is on the stream object once it is exhausted.
- `tool_call(role, messages, tools)`: one assistant turn of a tool loop. Returns the
  assistant message (ready to append to `messages`), the parsed tool calls and usage.

Every call: model, hard `max_tokens`, temperature and timeout come from config.yaml
`models.<role>`; `max_retries` is low; the budget is checked first (`BudgetRefused`, nothing is
sent) and the usage is booked afterwards.

The API key is read through `app.config.openrouter_key()` at call time. It is never logged,
and errors are re-raised as `LLMError` with a sanitised message and no exception chain, so a
traceback cannot carry the request or its headers.

Dev override for the degraded-mode check: `SIGNAL_DESK_INVALID_KEY_FOR_S=N` in the environment
makes the client use an invalid key for the first N seconds after start-up, then the real one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

import openai
from openai import AsyncOpenAI

from app.config import load_config, openrouter_key
from app.llm.budget import Budget, BudgetRefused, Reservation

log = logging.getLogger("signal_desk.llm")
for _name in ("httpx", "httpx2", "openai"):                # no per-request lines in the log
    logging.getLogger(_name).setLevel(logging.WARNING)

MAX_RETRIES = 1
_INVALID_KEY = "sk-or-v1-invalid-dev-override"
_STARTED = time.monotonic()


# ---------------------------------------------------------------- errors

class LLMError(Exception):
    """A model call failed. `kind`: auth | rate_limit | timeout | connection | bad_request |
    server | empty | parse | no_key | error. The message is safe to log."""

    def __init__(self, kind: str, message: str = "", status: Optional[int] = None):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.status = status


def _scrub(text: str) -> str:
    key = openrouter_key()
    if key:
        text = text.replace(key, "***")
    text = re.sub(r"sk-or-[A-Za-z0-9_\-]{8,}", "***", text)
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)
    return text[:300]


def _wrap(e: BaseException) -> LLMError:
    status = getattr(e, "status_code", None)
    if isinstance(e, openai.AuthenticationError) or status in (401, 403):
        kind = "auth"
    elif isinstance(e, openai.RateLimitError) or status == 429:
        kind = "rate_limit"
    elif isinstance(e, openai.APITimeoutError):
        kind = "timeout"
    elif isinstance(e, openai.APIConnectionError):
        kind = "connection"
    elif isinstance(e, openai.BadRequestError) or status in (400, 404, 422):
        kind = "bad_request"
    elif isinstance(e, openai.InternalServerError) or (status or 0) >= 500:
        kind = "server"
    else:
        kind = "error"
    return LLMError(kind, _scrub(f"{type(e).__name__}: {e}"), status)


# ---------------------------------------------------------------- JSON parsing

def extract_json(text: str) -> Optional[dict]:
    """Parse a JSON object from model text: plain JSON, a ```json fenced block, a bare fenced
    block, or the first {...} span (from ai-customers-workshop `_extract_json`). None if none."""
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    for marker in ("```json", "```"):
        if marker in text:
            block = text.split(marker, 1)[1].split("```", 1)[0]
            try:
                v = json.loads(block.strip())
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            v = json.loads(text[start:end + 1])
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    return None


def parse_json_loosely(content: str) -> dict:
    """Parse JSON from model output wrapped in fences, preceded by <think> blocks or surrounded
    by prose (from localdesk). Tries `extract_json`, then the first balanced {...} object.
    Raises json.JSONDecodeError if there is no object."""
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL).strip()
    v = extract_json(content)
    if v is not None:
        return v
    start = content.find("{")
    if start == -1:
        raise json.JSONDecodeError("no JSON object found", content, 0)
    depth, in_str, esc = 0, False, False
    for i in range(start, len(content)):
        ch = content[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                v = json.loads(content[start:i + 1])
                if isinstance(v, dict):
                    return v
                break
    raise json.JSONDecodeError("unbalanced JSON object", content, start)


# ---------------------------------------------------------------- reasoning filter

class ThinkFilter:
    """Streaming filter that drops inline <think>...</think> (and <thinking>...</thinking>)
    spans, even when a tag is split across deltas."""

    _OPEN = ("<think>", "<thinking>")
    _CLOSE = ("</think>", "</thinking>")

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False

    def feed(self, text: str) -> str:
        self._buf += text or ""
        out = []
        while self._buf:
            if self._inside:
                idx = [(self._buf.find(t), t) for t in self._CLOSE if t in self._buf]
                if idx:
                    i, t = min(idx)
                    self._buf = self._buf[i + len(t):]
                    self._inside = False
                    continue
                # keep a possible partial closing tag, drop the rest
                keep = max((len(t) - 1 for t in self._CLOSE), default=0)
                self._buf = self._buf[-keep:] if len(self._buf) > keep else self._buf
                break
            idx = [(self._buf.find(t), t) for t in self._OPEN if t in self._buf]
            if idx:
                i, t = min(idx)
                out.append(self._buf[:i])
                self._buf = self._buf[i + len(t):]
                self._inside = True
                continue
            # emit all but a possible partial opening tag at the end
            lt = self._buf.rfind("<")
            if lt != -1 and any(t.startswith(self._buf[lt:]) for t in self._OPEN):
                out.append(self._buf[:lt])
                self._buf = self._buf[lt:]
            else:
                out.append(self._buf)
                self._buf = ""
            break
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self._inside else self._buf
        self._buf = ""
        return rest


def delta_text(delta: Any) -> str:
    """The visible text of one stream delta. Reasoning parts (`reasoning`, `reasoning_content`,
    `reasoning_details`, `thinking`) are separate fields and are ignored on purpose."""
    if delta is None:
        return ""
    content = getattr(delta, "content", None) if not isinstance(delta, dict) else delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):          # content parts: keep text, skip thinking / reasoning parts
        out = []
        for part in content:
            p = part if isinstance(part, dict) else getattr(part, "__dict__", {})
            if p.get("type") in ("text", "output_text") and isinstance(p.get("text"), str):
                out.append(p["text"])
        return "".join(out)
    return ""


# ---------------------------------------------------------------- results

@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    estimated: bool = False


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str
    parse_error: Optional[str] = None


@dataclass
class ToolTurn:
    message: dict[str, Any]                # assistant message to append to `messages`
    content: str
    tool_calls: list[ToolCall]
    finish_reason: Optional[str]
    usage: Usage


class TextStream:
    """Async iterator of text deltas. After it is exhausted: `.text` (all visible text) and
    `.usage`. Raises LLMError / BudgetRefused like the other calls."""

    def __init__(self, client: "LLMClient", role: str, messages: list[dict], kw: dict[str, Any]):
        self._client = client
        self.role = role
        self.messages = messages
        self._kw = kw
        self.text = ""
        self.usage = Usage()

    def __aiter__(self) -> AsyncIterator[str]:
        return self._run()

    async def _run(self) -> AsyncIterator[str]:
        c = self._client
        res = c.budget.reserve(self.role, self._kw.pop("session", None), self._kw.pop("max_tokens", None))
        kwargs = c._kwargs(self.role, res, self._kw)
        kwargs.update(stream=True, stream_options={"include_usage": True})
        filt = ThinkFilter()
        pt = ct = 0
        got_usage = False
        try:
            stream = await c._api().chat.completions.create(messages=self.messages, **kwargs)
            async for chunk in stream:
                u = getattr(chunk, "usage", None)
                if u:
                    pt, ct, got_usage = int(u.prompt_tokens or 0), int(u.completion_tokens or 0), True
                for ch in (getattr(chunk, "choices", None) or []):
                    piece = filt.feed(delta_text(getattr(ch, "delta", None)))
                    if piece:
                        self.text += piece
                        yield piece
            tail = filt.flush()
            if tail:
                self.text += tail
                yield tail
        except (LLMError, BudgetRefused):
            c.budget.record(res, ok=False)
            raise
        except Exception as e:  # noqa: BLE001 - re-raised sanitised, without the chain
            c.budget.record(res, ok=False)
            raise _wrap(e) from None
        if not got_usage:
            pt = sum(len(str(m.get("content") or "")) for m in self.messages) // 4
            ct = len(self.text) // 4
        rec = c.budget.record(res, pt, ct, ok=bool(self.text.strip()), estimated=not got_usage)
        self.usage = Usage(rec.prompt_tokens, rec.completion_tokens, rec.cost_usd, rec.model, rec.estimated)
        if not self.text.strip():
            raise LLMError("empty", "stream returned no visible text")


# ---------------------------------------------------------------- client

class LLMClient:
    def __init__(self, budget: Budget, cfg: Optional[dict[str, Any]] = None,
                 key_fn: Callable[[], str] = openrouter_key, api_factory: Optional[Callable[[str], Any]] = None):
        self.budget = budget
        self.cfg = cfg or load_config()
        self.base_url = self.cfg.get("openrouter", {}).get("base_url", "https://openrouter.ai/api/v1")
        self._key_fn = key_fn
        self._api_factory = api_factory
        self._apis: dict[str, Any] = {}

    # ------------------------------------------------------------ plumbing
    def _key(self) -> str:
        n = os.environ.get("SIGNAL_DESK_INVALID_KEY_FOR_S", "").strip()
        if n:
            try:
                if time.monotonic() - _STARTED < float(n):
                    return _INVALID_KEY
            except ValueError:
                pass
        return self._key_fn()

    def _api(self) -> Any:
        key = self._key()
        if not key:
            raise LLMError("no_key", "OPENROUTER_API_KEY is not set")
        slot = "invalid" if key == _INVALID_KEY else "real"
        if slot not in self._apis:
            if self._api_factory is not None:
                self._apis[slot] = self._api_factory(key)
            else:
                self._apis[slot] = AsyncOpenAI(
                    api_key=key, base_url=self.base_url, max_retries=MAX_RETRIES,
                    default_headers={"X-Title": "Signal Desk"},
                )
        return self._apis[slot]

    def role_spec(self, role: str) -> dict[str, Any]:
        return self.cfg["models"][role]

    def _kwargs(self, role: str, res: Reservation, extra: dict[str, Any]) -> dict[str, Any]:
        spec = self.role_spec(role)
        extra = dict(extra)
        temp = extra.pop("temperature", None)
        timeout = extra.pop("timeout", None)
        kw: dict[str, Any] = {
            "model": spec["id"],
            "max_tokens": res.max_tokens,
            "temperature": float(spec.get("temperature", 0.2)) if temp is None else float(temp),
            "timeout": float(timeout) if timeout else float(spec.get("timeout_s", 30)),
        }
        kw.update(extra)
        return kw

    async def _create(self, role: str, messages: list[dict], *, session: Optional[str], max_tokens: Optional[int],
                      **extra: Any) -> tuple[Any, Usage]:
        res = self.budget.reserve(role, session, max_tokens)
        kwargs = self._kwargs(role, res, extra)
        try:
            resp = await self._api().chat.completions.create(messages=messages, **kwargs)
        except LLMError:
            self.budget.record(res, ok=False)
            raise
        except Exception as e:  # noqa: BLE001 - re-raised sanitised, without the chain
            self.budget.record(res, ok=False)
            raise _wrap(e) from None
        u = getattr(resp, "usage", None)
        pt = int(getattr(u, "prompt_tokens", 0) or 0) if u else 0
        ct = int(getattr(u, "completion_tokens", 0) or 0) if u else 0
        estimated = u is None
        if estimated:
            pt = sum(len(str(m.get("content") or "")) for m in messages) // 4
            try:
                ct = len(resp.choices[0].message.content or "") // 4
            except Exception:  # noqa: BLE001
                ct = 0
        ok = bool(getattr(resp, "choices", None))
        rec = self.budget.record(res, pt, ct, ok=ok, estimated=estimated)
        return resp, Usage(rec.prompt_tokens, rec.completion_tokens, rec.cost_usd, rec.model, estimated)

    # ------------------------------------------------------------ call shapes
    async def json_call(self, role: str, messages: list[dict], *, session: Optional[str] = None,
                        max_tokens: Optional[int] = None, temperature: Optional[float] = None,
                        timeout: Optional[float] = None) -> tuple[dict, Usage]:
        """One JSON object from the model. Raises LLMError (kinds `empty`, `parse`, transport
        kinds) or BudgetRefused."""
        common = dict(session=session, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
        try:
            resp, usage = await self._create(role, messages, response_format={"type": "json_object"}, **common)
        except LLMError as e:
            if e.kind != "bad_request":
                raise
            log.warning("json_object mode rejected for role %s (%s); retrying prompt-only", role, e.kind)
            resp, usage = await self._create(role, messages, **common)
        content = _message_text(resp)
        finish = getattr(resp.choices[0], "finish_reason", None) if getattr(resp, "choices", None) else None
        if not content.strip():
            raise LLMError("empty", f"model returned no content (finish_reason={finish})")
        try:
            return parse_json_loosely(content), usage
        except (json.JSONDecodeError, ValueError):
            kind = "truncated" if finish == "length" else "parse"
            raise LLMError(kind, f"no JSON object in {len(content)} chars of output "
                                 f"({usage.completion_tokens} tokens, finish_reason={finish})") from None

    def stream_text(self, role: str, messages: list[dict], *, session: Optional[str] = None,
                    max_tokens: Optional[int] = None, temperature: Optional[float] = None,
                    timeout: Optional[float] = None) -> TextStream:
        """`async for delta in client.stream_text(...)`; then `.text` and `.usage`."""
        return TextStream(self, role, messages, dict(session=session, max_tokens=max_tokens,
                                                     temperature=temperature, timeout=timeout))

    async def tool_call(self, role: str, messages: list[dict], tools: list[dict], *,
                        tool_choice: Any = "auto", session: Optional[str] = None,
                        max_tokens: Optional[int] = None, temperature: Optional[float] = None,
                        timeout: Optional[float] = None, parallel_tool_calls: Optional[bool] = None) -> ToolTurn:
        """One assistant turn with tools (OpenAI function-tool format). The returned
        `message` can be appended to `messages` as is; answer each `tool_calls[i]` with
        {"role": "tool", "tool_call_id": tc.id, "content": "..."}."""
        extra: dict[str, Any] = {"tools": tools, "tool_choice": tool_choice}
        if parallel_tool_calls is not None:
            extra["parallel_tool_calls"] = parallel_tool_calls
        resp, usage = await self._create(role, messages, session=session, max_tokens=max_tokens,
                                         temperature=temperature, timeout=timeout, **extra)
        choice = resp.choices[0]
        msg = choice.message
        content = _message_text(resp)
        calls: list[ToolCall] = []
        raw_calls: list[dict[str, Any]] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            fn = tc.function
            raw = fn.arguments or ""
            err = None
            try:
                args = parse_json_loosely(raw) if raw.strip() else {}
            except (json.JSONDecodeError, ValueError):
                args, err = {}, "arguments are not a JSON object"
            calls.append(ToolCall(id=tc.id, name=fn.name, arguments=args, raw_arguments=raw, parse_error=err))
            raw_calls.append({"id": tc.id, "type": "function", "function": {"name": fn.name, "arguments": raw}})
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if raw_calls:
            message["tool_calls"] = raw_calls
        if not calls and not content.strip():
            raise LLMError("empty", "model returned neither text nor tool calls")
        return ToolTurn(message=message, content=content, tool_calls=calls,
                        finish_reason=getattr(choice, "finish_reason", None), usage=usage)


def _message_text(resp: Any) -> str:
    try:
        msg = resp.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return ""
    text = delta_text(msg)
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL).strip()
