"""Headless end-to-end test of the gpt-live-1 voice channel — no browser, no mic.

Synthesises a caller utterance with TTS, streams it into a Live session over
the primary WebSocket, and lets the SAME LiveBridge that serves the web UI
handle the delegation (guardrails + tools + judge). Prints the transcript,
delegation stages and the spoken answer. Also handy in a lecture: it shows
the whole protocol in ~30 seconds of log lines.

    ./venv/bin/python scripts/live_smoke.py "What is the response time for a gold critical incident?"
    ./venv/bin/python scripts/live_smoke.py --seconds 40 "..."   # how long to listen
"""

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import OPENAI_API_KEY  # noqa: E402  (loads .env)
from app.voice_live import LiveBridge, _openai_client, live_session_config  # noqa: E402
from app.ws_hub import hub  # noqa: E402

RATE = 24000
CHUNK_MS = 100


async def tts_pcm(client, text: str) -> bytes:
    resp = await client.audio.speech.create(
        model="gpt-4o-mini-tts", voice="alloy", input=text, response_format="pcm",
    )
    return resp.content  # raw PCM16LE mono 24 kHz


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("utterance", nargs="?", default="Hi, what is the guaranteed response time for a critical incident on the gold tier?")
    ap.add_argument("--seconds", type=int, default=45, help="listen this long after speaking")
    ap.add_argument("--pause", type=float, default=6.0, help="silence between caller turns (s)")
    ap.add_argument("--approve", type=float, metavar="S", help="play the human: click Approve S seconds after a confirmation window opens")
    ap.add_argument("--deny", type=float, metavar="S", help="play the human: click Deny S seconds after a confirmation window opens")
    args = ap.parse_args()
    if not OPENAI_API_KEY:
        sys.exit("OPENAI_API_KEY missing in .env")

    client = _openai_client()
    t_start = time.time()
    stamp = lambda: f"{time.time() - t_start:6.1f}s"

    # Capture what the bridge would push to the web UI.
    async def fake_broadcast(msg_type, data):
        if msg_type == "voice_delegation":
            keys = {k: v for k, v in data.items() if k in ("stage", "request", "name", "ms", "judge_verdict", "guardrail_triggers", "blocked_by", "response", "summary", "decision", "wait_ms", "awaiting")}
            print(f"{stamp()}  [ui] {msg_type} {json.dumps(keys, ensure_ascii=False)[:400]}")
        else:
            print(f"{stamp()}  [ui] {msg_type} {json.dumps(data, ensure_ascii=False)[:200]}")
        # No browser here, so this script plays the human in front of the
        # confirmation window (without --approve/--deny the gate fails closed).
        if msg_type == "approval_requested" and human_delay is not None:
            async def click():
                await asyncio.sleep(human_delay)
                print(f"{stamp()}  [human] clicks {human_decision.upper()} on {data['action']} {data['target']}")
                approvals.resolve(data["id"], human_decision)
            asyncio.create_task(click())

    from app import approvals
    human_delay = args.approve if args.approve is not None else args.deny
    human_decision = "approved" if args.approve is not None else "denied"
    if human_delay is not None:
        approvals.approver_present = lambda: True
    hub.broadcast = fake_broadcast
    hub.set_loop(asyncio.get_running_loop())

    # Several caller turns can be given as "first || second"; a pause of
    # silence is inserted between them so the model gets to answer in between.
    turns = [t.strip() for t in args.utterance.split("||") if t.strip()]
    print(f"{stamp()}  synthesising {len(turns)} caller turn(s)…")
    pcms = [await tts_pcm(client, t) for t in turns]
    print(f"{stamp()}  {sum(len(p) for p in pcms) / 2 / RATE:.1f}s of audio")

    cfg = live_session_config(with_client_policy=False)
    cfg["audio"] = {"format": {"type": "audio/pcm", "rate": RATE}, "output": cfg["audio"]["output"]}

    async with client.live.connect() as conn:
        await conn.send({"type": "session.start", "session": cfg})
        bridge = LiveBridge("smoke", "smoke")
        user_line, asst_line = "", ""
        deadline = None
        audio_task = None

        async def feed_audio():
            step = RATE * 2 * CHUNK_MS // 1000
            silence = base64.b64encode(b"\x00" * step).decode()

            async def feed_silence(seconds: float):
                for _ in range(int(seconds * 1000 / CHUNK_MS)):
                    await conn.send({"type": "session.input_audio.append", "audio": silence})
                    await asyncio.sleep(CHUNK_MS / 1000)

            for n, pcm in enumerate(pcms):
                if n:
                    await feed_silence(args.pause)
                for i in range(0, len(pcm), step):
                    await conn.send({"type": "session.input_audio.append", "audio": base64.b64encode(pcm[i:i + step]).decode()})
                    await asyncio.sleep(CHUNK_MS / 1000)
            # Keep the "microphone" open: the session timeline only advances
            # with input audio, and context appends are injected on that
            # timeline. Without continuous silence the backend's answer would
            # never be spoken (error: context_injection_incomplete).
            while True:
                await feed_silence(1.0)

        async for ev in conn:
            t = ev.type
            if t == "session.started":
                sid = getattr(ev.session, "id", "?")
                print(f"{stamp()}  session.started id={sid}")
                audio_task = asyncio.create_task(feed_audio())
                deadline = time.time() + args.seconds
            elif t == "session.input_transcript.delta":
                user_line += ev.delta
                print(f"{stamp()}  caller ▸ {user_line.strip()}")
            elif t == "session.output_transcript.delta":
                asst_line += ev.delta
                print(f"{stamp()}  mu     ▸ {asst_line.strip()}")
            elif t == "session.delegation.created":
                print(f"{stamp()}  ⟶ delegation {ev.delegation.id} target={ev.delegation.target}")
            elif t == "session.output_audio.delta":
                pass  # would be played back in a real client
            elif t in ("session.thinking.appended", "session.commentary.appended", "session.instructions.appended"):
                print(f"{stamp()}  ✓ {t} ({ev.client_event_id})")
            elif t == "session.usage.updated":
                pass
            elif t == "error":
                print(f"{stamp()}  ✗ error {ev.error}")
            else:
                print(f"{stamp()}  · {t}")
            await bridge.handle_event(conn, ev)
            if t == "session.closed":
                break
            if deadline and time.time() > deadline:
                print(f"{stamp()}  closing…")
                if audio_task:
                    audio_task.cancel()
                await conn.send({"type": "session.close"})
                deadline = None
        if audio_task:
            audio_task.cancel()
        while bridge._pending:
            await asyncio.sleep(0.2)
        print(f"{stamp()}  done · {bridge.delegations} delegation(s) · usage {bridge.usage_seconds:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
