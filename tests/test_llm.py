"""LLM plumbing without the network: JSON parsing, reasoning filter, budget caps, the client's
fallbacks and error hygiene (a fake API object stands in for OpenRouter)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import httpx
import openai
import pytest

from app.config import load_config
from app.llm.budget import Budget, BudgetRefused
from app.llm.client import LLMClient, LLMError, ThinkFilter, delta_text, extract_json, parse_json_loosely

CFG = load_config()


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


def _cfg(**caps):
    c = json.loads(json.dumps(CFG))
    c["caps"].update(caps)
    return c


# ---------------------------------------------------------------- JSON parsing

@pytest.mark.parametrize("text", [
    '{"a": 1}',
    'Sure:\n```json\n{"a": 1}\n```',
    '```\n{"a": 1}\n```',
    'Here you go {"a": 1} hope it helps',
    '<think>let me think {"no": 0}</think>{"a": 1}',
])
def test_parse_json_loosely_variants(text):
    assert parse_json_loosely(text) == {"a": 1}


def test_parse_json_loosely_balanced_with_braces_in_strings():
    text = 'prefix {"t": "a } b", "n": {"x": [1, 2]}} trailing } junk {'
    assert parse_json_loosely(text) == {"t": "a } b", "n": {"x": [1, 2]}}


def test_parse_json_loosely_rejects_non_json():
    with pytest.raises(json.JSONDecodeError):
        parse_json_loosely("no json here")
    assert extract_json("[1, 2]") is None          # an array is not an object


# ---------------------------------------------------------------- reasoning filter

def test_think_filter_across_chunks():
    f = ThinkFilter()
    chunks = ["Hello <th", "ink>secret plan", " more</thi", "nk> world", " <thinking>x</thinking>!"]
    out = "".join(f.feed(c) for c in chunks) + f.flush()
    assert out == "Hello  world !"
    f2 = ThinkFilter()
    assert f2.feed("a < b and c <d") + f2.flush() == "a < b and c <d"


def test_delta_text_ignores_reasoning_fields():
    d = NS(content=None, reasoning="I should say hi", reasoning_details=[{"text": "x"}])
    assert delta_text(d) == ""
    assert delta_text(NS(content="hi", reasoning="hidden")) == "hi"
    parts = [{"type": "thinking", "text": "hidden"}, {"type": "text", "text": "shown"}]
    assert delta_text({"content": parts}) == "shown"


# ---------------------------------------------------------------- budget

def test_budget_cost_and_max_tokens():
    b = Budget(CFG, time_fn=Clock())
    assert b.max_tokens("triage") == CFG["models"]["triage"]["max_tokens"]
    assert b.max_tokens("triage", 10_000) == CFG["models"]["triage"]["max_tokens"]
    assert b.max_tokens("triage", 50) == 50
    r = b.reserve("triage")
    rec = b.record(r, 1_000_000, 1_000_000)
    pin, pout = b.price(CFG["models"]["triage"]["id"])
    assert rec.cost_usd == pytest.approx(pin + pout)
    assert b.snapshot()["totals"]["calls"] == 1


def test_budget_call_cap_refuses_and_sends_nothing():
    clk = Clock()
    b = Budget(_cfg(calls_per_hour_global=3), time_fn=clk)
    for _ in range(3):
        b.record(b.reserve("triage"), 10, 10)
    with pytest.raises(BudgetRefused) as e:
        b.reserve("triage")
    assert e.value.reason == "calls_per_hour_global" and e.value.retry_in_s > 0
    assert b.totals()["refused"] == 1 and b.totals()["calls"] == 3
    clk.t += 3601                                   # the window slides
    b.reserve("triage")


def test_budget_usd_and_session_caps():
    clk = Clock()
    b = Budget(_cfg(usd_per_hour_global=0.01, calls_per_hour_session=2), time_fn=clk)
    b.record(b.reserve("render", session="s1"), 1000, 100)
    b.record(b.reserve("render", session="s1"), 1000, 100)
    with pytest.raises(BudgetRefused) as e:
        b.reserve("render", session="s1")
    assert e.value.reason == "calls_per_hour_session"
    b.reserve("render", session="s2")              # another session is fine
    b.record(b.reserve("triage"), 30_000, 0)        # $0.012 at the triage price
    with pytest.raises(BudgetRefused) as e2:
        b.reserve("triage")
    assert e2.value.reason == "usd_per_hour_global"


def test_budget_cost_last_minute_window():
    clk = Clock()
    b = Budget(CFG, time_fn=clk)
    b.record(b.reserve("triage"), 100_000, 0)
    first = b.cost_last(60)
    assert first > 0
    clk.t += 61
    assert b.cost_last(60) == 0 and b.snapshot()["usd_last_hour"] == pytest.approx(first)


# ---------------------------------------------------------------- client with a fake API

def _resp(content, pt=100, ct=20, tool_calls=None):
    msg = NS(content=content, tool_calls=tool_calls)
    return NS(choices=[NS(message=msg, finish_reason="stop")], usage=NS(prompt_tokens=pt, completion_tokens=ct))


class FakeAPI:
    def __init__(self, handler):
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))
        self._handler = handler

    async def _create(self, **kw):
        self.calls.append(kw)
        return self._handler(kw, len(self.calls))


def _client(handler, cfg=CFG, key="sk-or-v1-testkey1234567890"):
    api = FakeAPI(handler)
    c = LLMClient(Budget(cfg, time_fn=Clock()), cfg, key_fn=lambda: key, api_factory=lambda k: api)
    return c, api


def _bad_request():
    return openai.BadRequestError("response_format not supported",
                                  response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)


def test_json_call_falls_back_to_prompt_only():
    def h(kw, n):
        if "response_format" in kw:
            raise _bad_request()
        return _resp('Here:\n```json\n{"attach": [], "new": [], "noise_ids": ["a"]}\n```')
    c, api = _client(h)
    out, usage = asyncio.run(c.json_call("triage", [{"role": "user", "content": "x"}]))
    assert out["noise_ids"] == ["a"]
    assert len(api.calls) == 2 and "response_format" not in api.calls[1]
    assert api.calls[0]["max_tokens"] == CFG["models"]["triage"]["max_tokens"]
    assert api.calls[0]["timeout"] == CFG["models"]["triage"]["timeout_s"]
    assert c.budget.totals()["calls"] == 2 and usage.prompt_tokens == 100


def test_json_call_empty_content_is_a_failure():
    c, _ = _client(lambda kw, n: _resp(""))
    with pytest.raises(LLMError) as e:
        asyncio.run(c.json_call("triage", [{"role": "user", "content": "x"}]))
    assert e.value.kind == "empty"


def test_budget_refusal_sends_nothing():
    cfg = _cfg(calls_per_hour_global=0)
    c, api = _client(lambda kw, n: _resp("{}"), cfg=cfg)
    with pytest.raises(BudgetRefused):
        asyncio.run(c.json_call("triage", [{"role": "user", "content": "x"}]))
    assert api.calls == []


def test_errors_never_carry_the_key():
    key = "sk-or-v1-supersecretkey0123456789"

    def h(kw, n):
        raise openai.AuthenticationError(f"bad key {key}",
                                         response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
                                         body=None)
    c, _ = _client(h, key=key)
    with pytest.raises(LLMError) as e:
        asyncio.run(c.json_call("triage", [{"role": "user", "content": "x"}]))
    assert e.value.kind == "auth"
    assert key not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_no_key_is_a_clean_error():
    c, api = _client(lambda kw, n: _resp("{}"), key="")
    with pytest.raises(LLMError) as e:
        asyncio.run(c.json_call("triage", [{"role": "user", "content": "x"}]))
    assert e.value.kind == "no_key" and api.calls == []


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def gen():
            for ch in self.chunks:
                yield ch
        return gen()


def _chunk(content=None, reasoning=None, usage=None):
    choices = [] if content is None and reasoning is None else [NS(delta=NS(content=content, reasoning=reasoning))]
    return NS(choices=choices, usage=usage)


def test_stream_text_filters_reasoning_and_reports_usage():
    chunks = [_chunk(reasoning="planning..."), _chunk("The core "), _chunk("<think>hmm</think>uplink "),
              _chunk("is down."), _chunk(usage=NS(prompt_tokens=50, completion_tokens=7))]
    c, api = _client(lambda kw, n: FakeStream(chunks))

    async def go():
        s = c.stream_text("render", [{"role": "user", "content": "x"}])
        got = [p async for p in s]
        return got, s
    got, s = asyncio.run(go())
    assert "".join(got) == "The core uplink is down." and s.text == "The core uplink is down."
    assert s.usage.prompt_tokens == 50 and s.usage.completion_tokens == 7 and not s.usage.estimated
    assert api.calls[0]["stream"] is True and api.calls[0]["stream_options"] == {"include_usage": True}
    assert c.budget.totals()["calls"] == 1


def test_tool_call_returns_appendable_message():
    tc = NS(id="call_1", function=NS(name="top_alerts", arguments='{"limit": 5}'))
    c, api = _client(lambda kw, n: _resp(None, tool_calls=[tc]))
    tools = [{"type": "function", "function": {"name": "top_alerts", "parameters": {"type": "object"}}}]
    turn = asyncio.run(c.tool_call("engineer", [{"role": "user", "content": "go"}], tools))
    assert turn.tool_calls[0].name == "top_alerts" and turn.tool_calls[0].arguments == {"limit": 5}
    assert turn.message["role"] == "assistant" and turn.message["tool_calls"][0]["id"] == "call_1"
    assert api.calls[0]["tools"] == tools and api.calls[0]["model"] == CFG["models"]["engineer"]["id"]
