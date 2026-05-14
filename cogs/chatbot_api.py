"""
chatbot_api.py — Standalone AI routing module for Friez.

Usage:
    import chatbot_api as api
    import asyncio

    # Chat with personality (stateful, remembers conversation per session_id)
    reply, backend, model, elapsed = await api.call("chatbot", "my-channel-123", "hey whats up", display_name="Philipp")

    # Logic query — no personality, just raw yes/no or factual answers
    reply, backend, model, elapsed = await api.call("logic", None, "is the word 'idiot' a swear word? reply only yes or no")

api.call() always returns a tuple: (reply: str, backend: str, model: str, elapsed: float)

Available modes:
    "chatbot"  — uses Friez personality, stateful chat history per session_id
    "logic"    — no personality, clean factual/boolean queries, stateless
"""

import os
import json
import time
import datetime
import asyncio
import requests
from dotenv import load_dotenv
from google import genai

load_dotenv()

# ── Gemini client ──────────────────────────────────────────────────────────────
_gemini_client = genai.Client(api_key=os.getenv("GEMINI_TOKEN"))

# ── Ollama config ──────────────────────────────────────────────────────────────
UBUNTU_OLLAMA_URL   = os.getenv("UBUNTU_OLLAMA_URL",  "http://192.168.30.10:11434")
UBUNTU_OLLAMA_MODEL = os.getenv("UBUNTU_OLLAMA_MODEL", "llama3.1:8b")
PI5_OLLAMA_URL      = os.getenv("PI5_OLLAMA_URL",     "http://192.168.30.80:11434")
PI5_OLLAMA_MODEL    = os.getenv("PI5_OLLAMA_MODEL",    "qwen2.5:3b")
OLLAMA_TIMEOUT      = int(os.getenv("OLLAMA_TIMEOUT", "5"))

# ── Personality — loaded from personality.json on startup ─────────────────────
_PERSONALITY_FILE = os.path.join(os.path.dirname(__file__), "personality.json")

_PERSONALITY_DEFAULTS = {
    "chatbot": "You are Friez. Be casual, dry, a little sarcastic. Never robotic. Keep replies short.",
    "ollama_chatbot": "You are Friez. Casual, dry, sarcastic. One or two sentences max. Never robotic."
}

def _load_personalities() -> dict:
    try:
        with open(_PERSONALITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        chatbot         = data.get("chatbot",         _PERSONALITY_DEFAULTS["chatbot"])
        ollama_chatbot  = data.get("ollama_chatbot",  _PERSONALITY_DEFAULTS["ollama_chatbot"])
        print(f"[chatbot_api] Loaded personality from {_PERSONALITY_FILE}")
        return {"chatbot": chatbot, "ollama_chatbot": ollama_chatbot}
    except FileNotFoundError:
        print(f"[chatbot_api] personality.json not found — using built-in defaults.")
        return dict(_PERSONALITY_DEFAULTS)
    except Exception as e:
        print(f"[chatbot_api] Failed to load personality.json: {e} — using built-in defaults.")
        return dict(_PERSONALITY_DEFAULTS)

_personalities = _load_personalities()
CHATBOT_PERSONALITY        = _personalities["chatbot"]
OLLAMA_CHATBOT_PERSONALITY = _personalities["ollama_chatbot"]

# ── Session state ──────────────────────────────────────────────────────────────
# Gemini chat sessions keyed by session_id
_gemini_sessions: dict[str, object] = {}

# Ollama history keyed by session_id: list of {"role": ..., "content": ...}
_ollama_history: dict[str, list[dict]] = {}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _ollama_ping(base_url: str) -> bool:
    try:
        r = requests.get(f"{base_url}/api/tags", timeout=OLLAMA_TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False


def _ollama_request(base_url: str, model: str, system: str,
                    history: list[dict], user_msg: str) -> str | None:
    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_msg})
    payload = {"model": model, "messages": messages, "stream": False, "keep_alive": "10m"}
    for attempt in range(3):
        try:
            with requests.Session() as s:
                r = s.post(
                    f"{base_url}/api/chat",
                    json=payload,
                    timeout=(10, 180),  # (connect timeout, read timeout)
                )
            if r.status_code == 200:
                return r.json().get("message", {}).get("content", "").strip()
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            print(f"[chatbot_api] Ollama {base_url} connection error (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                import time as _t; _t.sleep(2)
        except Exception as e:
            print(f"[chatbot_api] Ollama {base_url} error: {e}")
            break
    return None


def _ollama_append(session_id: str, role: str, content: str, max_turns: int = 20):
    hist = _ollama_history.setdefault(session_id, [])
    hist.append({"role": role, "content": content})
    if len(hist) > max_turns * 2:
        _ollama_history[session_id] = hist[-(max_turns * 2):]


def _gemini_session(session_id: str, system: str) -> object:
    """Get or create a Gemini chat session for this session_id + system prompt combo."""
    key = f"{session_id}::{hash(system)}"
    if key not in _gemini_sessions:
        _gemini_sessions[key] = _gemini_client.chats.create(
            model="gemini-2.5-flash-lite",
            config={"system_instruction": system}
        )
    return _gemini_sessions[key]


def _is_rate_limit(e: Exception) -> bool:
    return any(k in str(e).lower() for k in ("quota", "rate", "429", "resource_exhausted"))


# ── Core routing ───────────────────────────────────────────────────────────────

async def _route(system_prompt: str, session_id: str | None,
                 prompt: str, stateful: bool) -> tuple[str, str, str, float]:
    """
    Internal router. Returns (reply, backend, model, elapsed).
    stateful=True  → keeps chat history (chatbot mode)
    stateful=False → single-shot, no history (logic mode)
    """

    # ── 1. Gemini ──────────────────────────────────────────────────────────────
    try:
        loop = asyncio.get_event_loop()
        t0 = time.monotonic()
        if stateful and session_id:
            chat     = _gemini_session(session_id, system_prompt)
            response = await loop.run_in_executor(None, chat.send_message, prompt)
        else:
            def _one_shot():
                tmp = _gemini_client.chats.create(
                    model="gemini-2.5-flash-lite",
                    config={"system_instruction": system_prompt}
                )
                return tmp.send_message(prompt)
            response = await loop.run_in_executor(None, _one_shot)
        elapsed = time.monotonic() - t0
        return response.text.strip(), "Gemini API", "gemini-2.5-flash-lite", elapsed
    except Exception as e:
        if not _is_rate_limit(e):
            print(f"[chatbot_api] Gemini error: {e}")
            return "⚠️ AI hit an unexpected error. Please try again.", "Gemini API", "gemini-2.5-flash-lite", 0.0
        print("[chatbot_api] Gemini rate-limited — falling back to local LLMs.")

    # ── 2. Ubuntu Ollama ───────────────────────────────────────────────────────
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, _ollama_ping, UBUNTU_OLLAMA_URL):
        hist = _ollama_history.get(session_id, []) if (stateful and session_id) else []
        t0 = time.monotonic()
        reply = await loop.run_in_executor(
            None, lambda: _ollama_request(UBUNTU_OLLAMA_URL, UBUNTU_OLLAMA_MODEL, system_prompt, hist, prompt)
        )
        elapsed = time.monotonic() - t0
        if reply:
            if stateful and session_id:
                _ollama_append(session_id, "user", prompt)
                _ollama_append(session_id, "assistant", reply)
            return reply, "Ubuntu PC", UBUNTU_OLLAMA_MODEL, elapsed
        print("[chatbot_api] Ubuntu Ollama failed — trying Pi 5.")

    # ── 3. Pi 5 Ollama ─────────────────────────────────────────────────────────
    if await loop.run_in_executor(None, _ollama_ping, PI5_OLLAMA_URL):
        hist = _ollama_history.get(session_id, []) if (stateful and session_id) else []
        t0 = time.monotonic()
        reply = await loop.run_in_executor(
            None, lambda: _ollama_request(PI5_OLLAMA_URL, PI5_OLLAMA_MODEL, system_prompt, hist, prompt)
        )
        elapsed = time.monotonic() - t0
        if reply:
            if stateful and session_id:
                _ollama_append(session_id, "user", prompt)
                _ollama_append(session_id, "assistant", reply)
            return reply, "Pi 5", PI5_OLLAMA_MODEL, elapsed

    return "⚠️ All AI backends are currently unavailable. Try again soon.", "None", "N/A", 0.0


# ── Public API ─────────────────────────────────────────────────────────────────

async def call(mode: str, session_id: str | None, prompt: str,
               display_name: str | None = None) -> tuple[str, str, str, float]:
    """
    Main entry point.

    Parameters:
        mode        — "chatbot" or "logic" (or any custom system prompt string)
        session_id  — unique ID to scope chat history (e.g. Discord channel ID).
                      Pass None for stateless queries.
        prompt      — the user's message or query
        display_name — optional name tag prepended to the prompt in chatbot mode

    Returns:
        (reply: str, backend: str, model: str, elapsed: float)

    Examples:
        # Chatbot with personality
        reply, backend, model, elapsed = await api.call("chatbot", str(channel.id), "hey", display_name="Philipp")

        # Logic query — yes/no, factual, stateless
        reply, backend, model, elapsed = await api.call("logic", None, "is 'idiot' a swear word? reply only yes or no")

        # Custom system prompt
        reply, backend, model, elapsed = await api.call("translate everything to pirate speak", None, "hello how are you")
    """
    if mode == "chatbot":
        now = datetime.datetime.now()
        date_ctx = (
            f"\n\n═══ CURRENT CONTEXT ═══\n"
            f"Date: {now.strftime('%A, %d %B %Y')}\n"
            f"Time: {now.strftime('%H:%M')} (server local time)\n"
            f"Use this if someone asks what time/day it is. Don't mention it unprompted."
        )
        system = CHATBOT_PERSONALITY + date_ctx
        tagged = f"[{display_name}]: {prompt}" if display_name else prompt
        return await _route(system, session_id, tagged, stateful=True)

    elif mode == "logic":
        system = "You are a precise logic engine. Answer factually and concisely. No personality, no fluff, no extra explanation unless asked."
        return await _route(system, session_id, prompt, stateful=False)

    else:
        # Treat mode as a raw custom system prompt
        return await _route(mode, session_id, prompt, stateful=bool(session_id))


def clear_history(session_id: str):
    """Clear all stored history for a session (both Gemini and Ollama)."""
    _ollama_history.pop(session_id, None)
    # Clear any Gemini sessions matching this session_id
    to_del = [k for k in _gemini_sessions if k.startswith(f"{session_id}::")]
    for k in to_del:
        del _gemini_sessions[k]
