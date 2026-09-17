# Jarvis

A voice assistant that runs entirely on your own hardware. Speech recognition,
reasoning and speech synthesis are all local — no cloud model, no API keys, no
external agent framework. It controls your PCs, your smart home, and your
phone, and it answers from whichever device you happen to be near.

```
  Laptop 1          Laptop 2          Android
  wake + hotkey     wake + hotkey     wake + widget
  node agent        node agent        phone actions
       └────────────── Tailscale ──────────┘
                        │
                  DESKTOP = HUB
                  faster-whisper · Qwen3-14B · Piper
                  tool router · SQLite
                        │
                  Home Assistant + SearXNG (Docker)
```

The desktop is the hub because a local stack needs Whisper, a 14B model and
Piper resident in VRAM permanently — laptops sleep and move. Every other
device is a thin client that also accepts commands.

## What works today

Phase 1 of the build: the full voice pipeline end to end, with timers as the
proof-of-life tool. Phases 2–6 (PC control, wake word, smart home, assistant
tasks, Android) build on this without changing it.

---

## Setup

### 1. Prerequisites — on the desktop

| Thing | Why | Install |
|---|---|---|
| Python 3.13 | the hub | python.org (3.14 has no CTranslate2 wheels yet) |
| Ollama | the local LLM | <https://ollama.com/download> |
| Tailscale | reaches your other devices | <https://tailscale.com/download> |
| ffmpeg | audio decoding | `winget install Gyan.FFmpeg` |

```powershell
ollama pull qwen3:14b
ollama list                 # confirm it is there
tailscale ip -4             # note this address; it is your hub address
```

If the desktop has 24 GB+ of VRAM, pull `qwen3:32b` instead and change
`llm.model` in `hub/config.yaml`. If it has under 12 GB, drop
`stt.model` to `distil-large-v3` and `stt.compute_type` to `int8_float16`.

### 2. Install

```powershell
git clone <your-repo> jarvis
cd jarvis
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-hub.txt      # on the hub
pip install -r requirements-client.txt   # on every machine with a microphone
```

### 3. Register devices

Run this on the hub, once per device:

```powershell
python scripts/new_device.py desktop  --alias "the desktop"
python scripts/new_device.py laptop-1 --alias "bedroom laptop"
python scripts/new_device.py laptop-2 --alias "work laptop"
python scripts/new_device.py phone    --alias "my phone"
```

Each prints a token once. Put it on that machine as `client/token`. The
aliases are what you can say out loud — "open Chrome on the bedroom laptop".

### 4. Bind the hub to Tailscale

In `hub/config.yaml` set `server.host` to the address from `tailscale ip -4`.
Leaving it as `127.0.0.1` means only the desktop itself can connect, which is
right for first-run testing and wrong for everything after.

The hub never listens on the public internet, and never on the plain LAN.

### 5. Run

```powershell
# on the hub
python -m hub.server

# on each machine with a mic
python -m client.listener --hub ws://100.x.y.z:8080 --device laptop-1
```

First start downloads the Piper voice (~60 MB) and loads Whisper — expect
30–60 seconds. After that the models stay warm.

---

## Verifying it works

Test the brain with no microphone at all:

```powershell
python scripts/say.py "set a timer for ten seconds" --wait 15
```

You should see the reply, a latency figure, and the timer announcement ten
seconds later. Then check the hub's own view:

```powershell
curl http://127.0.0.1:8080/healthz
```

Then with voice: hold `ctrl+alt+space`, say "set a timer for two minutes",
release. The client logs the round-trip latency of every turn.

Save the spoken reply to a file if you want to check the audio itself:

```powershell
python scripts/say.py --save reply.wav "what timers are running"
```

### Tests

These need no hub, no models and no network:

```powershell
python scripts/test_guard.py     # allowlist: path traversal, app and close lists
python scripts/test_router.py    # Tier 1 hits, and what must NOT match
```

`test_router.py` is the one to watch. A false Tier 1 match silently does the
wrong thing, so its "must fall through to the LLM" cases matter more than the
hits.

With the hub running, measure latency:

```powershell
python scripts/bench.py          # p50/p95 per tier, against budgets
python scripts/bench.py --tier 1 # fast path only
```

Tier 1 budget is 500 ms p50, Tier 2 is 4 s. If Tier 1 drifts above budget the
central premise of the design has broken and it is worth fixing before adding
anything.

---

## Layout

```
hub/          runs on the desktop only
  server.py     WebSocket endpoints for voice clients and node agents
  stt.py        faster-whisper, warm and serialised behind a lock
  tts.py        Piper, streamed sentence by sentence
  scheduler.py  timers and reminders
  nodes.py      registry of connected machines, spoken-name resolution
  auth.py       per-device tokens
  brain/
    dispatch.py   confirmation -> Tier 1 -> Tier 2, plus the audit log
    router.py     Tier 1: deterministic fast path, no LLM
    agent.py      Tier 2: the Ollama tool-calling loop
    registry.py   @tool decorator; one declaration feeds both LLM and executor
    confirm.py    spoken confirmation for destructive actions
    tools/        one module per capability
client/       voice client: mic, wake word, hotkey, playback
node/         action executor; runs on all three PCs (Phase 2)
android/      Termux client (Phase 6)
scripts/      new_device.py, say.py
```

### Adding a capability

Write a function, decorate it, import the module in `brain/tools/__init__.py`.
Nothing else. The decorator's schema is what the LLM sees, so the description
is the prompt:

```python
@tool(
    name="lock_screen",
    description="Lock the screen on one of the user's computers.",
    parameters={
        "type": "object",
        "properties": {"device": {"type": "string"}},
    },
    confirm=False,
)
async def lock_screen(ctx: Context, device: str = "") -> ToolResult:
    target = ctx.nodes.resolve(device)
    if target is None:
        return ToolResult.fail("I'm not sure which machine you mean.")
    await ctx.nodes.send(target, "lock", {})
    return ToolResult.say(f"Locked {target}.")
```

Set `confirm=True` for anything destructive or outward-facing — deleting
files, sending mail or SMS. Jarvis will then read the action back and wait for
a spoken yes.

Pass `available=` when a tool depends on something that may not be there:

```python
@tool(name="...", description="...", available=phone_connected)
```

The catalogue is rebuilt per turn, so a tool whose predicate is false is never
offered to the model. This matters more than it looks: the full 34-tool
catalogue is ~2,900 tokens on every Tier 2 call, versus ~930 with nothing
connected. On a 14B model that difference shows up in both latency and how
often it picks the right tool.

---

## Design notes

**Two tiers.** A 14B model takes 1.5–3 s to emit a tool call, which is
unacceptable for "turn off the lights". `brain/router.py` answers the common
commands deterministically in under half a second; everything else falls
through to the LLM. The audit log records which Tier 2 resolutions are
frequent, so they can be promoted into Tier 1.

**Why the reply often skips a second LLM round.** When exactly one tool is
called and it succeeds, its own spoken confirmation is returned directly
rather than asking the model to summarise it. That saves around 1.5 s on the
overwhelmingly common case.

**Thinking mode is off.** Qwen3 will otherwise spend hundreds of tokens
reasoning about a light switch.

**Security.** The hub binds to Tailscale only; every device carries its own
token; node agents work from an allowlist of permitted apps and writable
paths rather than a blocklist; arbitrary shell is disabled in config;
destructive actions need spoken confirmation; every tool call is written to
`logs/audit.jsonl`.

## Known limits

- **Alexa and Google Home devices are cloud-locked** and cannot be controlled
  locally. Tuya/Smart Life and TP-Link gear re-pairs into Home Assistant and
  works properly. Echo-only devices need the unofficial `alexa_media_player`
  integration, which breaks whenever Amazon changes their login flow.
- **Gmail and Calendar are cloud services.** "Fully local" governs the models,
  not your mail provider. Notes, lists, reminders and timers are local.
- **A 14B local model is not Claude.** It handles a small, sharply-defined tool
  catalogue well and vague open-ended requests poorly. That is what the tool
  descriptions are for.
- **iPhone is not supported.** iOS allows neither a wake word nor deep app
  control without jailbreaking. An Apple Shortcut posting to the hub is the
  realistic path, once the rest is proven.
