"""Free voice "call" inside Telegram (Telegram Mini App) - no Twilio needed.

The /call button in the bot opens web/webcall.html inside Telegram. The page records
the customer's voice and sends each phrase here:

  GET  /webcall        - the call screen
  POST /webcall/start  - checks the Telegram user, returns the spoken greeting
  POST /webcall/turn   - customer's phrase (audio) -> Whisper -> Claude -> voice (edge-tts)
  POST /webcall/end    - the call was hung up: the conversation is saved to the database

Speech recognition (faster-whisper) runs on this computer, the voice is made with edge-tts.
Both are free; only Claude's answers are paid.
"""
import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
import threading
import time
import uuid
from urllib.parse import parse_qsl

import av
import edge_tts
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

import assistant
import config
import limits

VOICE = "uk-UA-PolinaNeural"     # or "uk-UA-OstapNeural" for a male voice
VOICE_RATE = "+0%"               # speed: "+15%" faster, "-10%" slower
VOICE_PITCH = "+0Hz"             # pitch: "+10Hz" higher, "-10Hz" lower
WHISPER_MODEL = "small"          # "medium" recognizes better but is ~3x slower
WHISPER_HINTS = "Беррі Фарм, саджанці, лохина, гортензія, павловнія, ківі, ожина, фундук, Нова Пошта."
MAX_AUDIO_BYTES = 5 * 1024 * 1024
SESSION_TIMEOUT = 30 * 60        # a call nobody touched for 30 minutes is saved and closed
PAGE = os.path.join(assistant.BASE_DIR, "web", "webcall.html")

router = APIRouter()
sessions = {}  # session id -> {"history", "chat_id", "username", "last_seen"}
on_call_saved = None  # set by main.py: function(chat_id, data) to notify the customer in Telegram

# ---------------------------------------------------------------------------
# Speech recognition (customer's voice -> text)
# ---------------------------------------------------------------------------

_whisper = None
_load_lock = threading.Lock()     # the model is loaded only once
_whisper_lock = threading.Lock()  # one recognition at a time: the model is not thread-safe on CPU


def get_whisper():
    global _whisper
    with _load_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        return _whisper


def decode_audio(audio_bytes):
    """webm (Android) / mp4 (iPhone) -> 16 kHz mono samples for Whisper.
    (faster-whisper's own decoder doesn't work with the installed PyAV version.)"""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    with av.open(io.BytesIO(audio_bytes)) as container:
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray() for f in resampler.resample(frame)]
    chunks += [f.to_ndarray() for f in resampler.resample(None)]
    if not chunks:
        return None
    return np.concatenate(chunks, axis=1).flatten().astype(np.float32) / 32768.0


def speech_to_text(audio_bytes):
    try:
        samples = decode_audio(audio_bytes)
    except av.FFmpegError as e:
        print(f"Could not read the recorded audio: {e}")
        return ""
    if samples is None:
        return ""
    model = get_whisper()
    with _whisper_lock:
        segments, _ = model.transcribe(
            samples, language="uk", beam_size=1,
            vad_filter=True, initial_prompt=WHISPER_HINTS,
        )
        return " ".join(s.text.strip() for s in segments).strip()


def preload_whisper():
    """Loads the model in the background at startup so the first call isn't slow."""
    threading.Thread(target=get_whisper, daemon=True).start()

# ---------------------------------------------------------------------------
# Voice (AI's text -> mp3)
# ---------------------------------------------------------------------------


async def text_to_speech(text):
    text = re.sub(r"[^\w\s.,!?;:'’\"«»()\-–—%№/+]", "", text)  # drop emojis and markdown
    audio = b""
    async for chunk in edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH).stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return base64.b64encode(audio).decode()

# ---------------------------------------------------------------------------
# Telegram user check
# ---------------------------------------------------------------------------


def telegram_user(init_data):
    """Checks that the page was opened from our bot (Telegram signs initData with the bot token)."""
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", config.API_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not received_hash or not hmac.compare_digest(expected, received_hash):
        return None
    if time.time() - int(fields.get("auth_date", 0)) > 24 * 3600:
        return None
    return json.loads(fields.get("user", "{}"))

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/webcall")
async def page():
    return FileResponse(PAGE, media_type="text/html")


@router.post("/webcall/start")
async def start(request: Request):
    body = await request.json()
    user = telegram_user(body.get("initData", ""))
    if not user:
        raise HTTPException(403, "Open the call from the Telegram bot")

    await asyncio.to_thread(close_stale_sessions)
    user_id = user.get("id")
    seconds_left = await asyncio.to_thread(limits.call_seconds_left, user_id)
    if seconds_left < 30 or not await asyncio.to_thread(limits.start_conversation, user_id):
        raise HTTPException(429, assistant.TEXTS["limit_reached"])

    greeting = assistant.TEXTS["voice_greeting"]
    session_id = uuid.uuid4().hex
    sessions[session_id] = {
        "history": [
            {"role": "user", "content": assistant.TEXTS["call_start"]},
            {"role": "assistant", "content": greeting},
        ],
        "chat_id": user_id,
        "username": user.get("username", ""),
        "started": time.time(),
        "seconds_left": seconds_left,
        "last_seen": time.time(),
    }
    print(f"[webcall {session_id[:8]}] Call started by {user.get('id')}")
    return {"session": session_id, "text": greeting, "audio": await text_to_speech(greeting)}


@router.post("/webcall/turn")
async def turn(session: str = Form(...), audio: UploadFile = File(...)):
    state = sessions.get(session)
    if state is None:
        raise HTTPException(404, "The call has ended")
    state["last_seen"] = time.time()

    audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Phrase is too long")

    heard = await asyncio.to_thread(speech_to_text, audio_bytes)
    if not heard:
        text = assistant.TEXTS["voice_no_input"]
        return {"heard": "", "text": text, "audio": await text_to_speech(text), "finished": False}
    print(f"[webcall {session[:8]}] Client: {heard}")

    history = state["history"]
    history.append({"role": "user", "content": heard})

    user_messages = sum(m["role"] == "user" for m in history) - 1  # minus the automatic opening
    out_of_time = time.time() - state["started"] > state["seconds_left"]
    if out_of_time or not await asyncio.to_thread(limits.add_message, state["chat_id"], user_messages):
        text = assistant.TEXTS["limit_reached_voice"]
        history.append({"role": "assistant", "content": text})
        finish_in_background(session)
        return {"heard": heard, "text": text, "audio": await text_to_speech(text), "finished": True}

    try:
        reply, finished = await asyncio.to_thread(assistant.chat_reply, history, "phone")
    except Exception as e:
        print(f"[webcall {session[:8]}] Claude error: {e}")
        reply, finished = None, False
    if not reply:
        history.pop()
        text = assistant.TEXTS["voice_error"]
        return {"heard": heard, "text": text, "audio": await text_to_speech(text), "finished": False}

    history.append({"role": "assistant", "content": reply})
    print(f"[webcall {session[:8]}] AI: {reply}")
    audio_b64 = await text_to_speech(reply)
    if finished:
        finish_in_background(session)
    return {"heard": heard, "text": reply, "audio": audio_b64, "finished": finished}


@router.post("/webcall/end")
async def end(request: Request):
    try:
        session = (await request.json()).get("session", "")
    except ValueError:  # sendBeacon from a closing page may send plain text
        session = (await request.body()).decode(errors="ignore")
    finish_in_background(session)
    return {"ok": True}

# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def finish_in_background(session_id):
    state = sessions.pop(session_id, None)
    if not state:
        return
    print(f"[webcall {session_id[:8]}] Call ended")

    def work():
        limits.add_call_seconds(state["chat_id"], time.time() - state["started"])
        data = assistant.finish_conversation(
            state["history"], "webcall",
            chat_id=state["chat_id"], username=state["username"],
        )
        if data and state["chat_id"] and on_call_saved:
            on_call_saved(state["chat_id"], data)

    threading.Thread(target=work, daemon=True).start()


def close_stale_sessions():
    now = time.time()
    for session_id, state in list(sessions.items()):
        if now - state["last_seen"] > SESSION_TIMEOUT:
            finish_in_background(session_id)
