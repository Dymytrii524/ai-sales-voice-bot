"""Real-time phone calls through Twilio ConversationRelay.

Based on https://github.com/twilio-samples/ai-phone-agent-conversation-relay-python
(OpenAI replaced with Claude).

  POST /twiml - the call was answered: Twilio speaks the greeting and opens the WebSocket
  WS   /ws    - live conversation: Twilio sends the customer's recognized speech ("prompt"),
                we stream Claude's answer back word by word ("text" tokens) and Twilio speaks it.
                The customer can interrupt the AI at any moment ("interrupt").

Speech recognition (Deepgram) and the voice (ElevenLabs) are done by Twilio in real time.
When the call ends, the conversation is saved to the database (assistant.finish_conversation).
"""
import asyncio
import json

import anthropic
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

import assistant
import config

TWILIO_ACCOUNT_SID = getattr(config, "TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = getattr(config, "TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = getattr(config, "TWILIO_PHONE_NUMBER", "")
PUBLIC_URL = getattr(config, "PUBLIC_URL", "").rstrip("/")
PORT = getattr(config, "PHONE_SERVER_PORT", 8000)

MODEL = assistant.MODEL   # a faster model ("claude-haiku-5-5") shortens the pause before each answer
LANGUAGE = "uk-UA"
CHARS_PER_SECOND = 14     # rough speech speed, used to let the goodbye finish before hanging up

app = FastAPI()
sessions = {}  # CallSid -> {"history", "system", "greeting", "channel", "chat_id", "username", "phone"}
on_call_saved = None  # set by main.py: function(chat_id, data) to notify the customer in Telegram


def is_configured():
    return all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, PUBLIC_URL])


def new_session(chat_id=None, username="", phone="", order=None):
    """order=None - sales call; order=dict (from extract_data) - call to confirm that order."""
    if order:
        start = assistant.TEXTS["confirm_call_start"] + "\n" + assistant.format_order(order)
        greeting = assistant.TEXTS["voice_greeting_confirm"]
        system, channel = assistant.CONFIRM_PROMPT, "phone_confirm"
    else:
        start, greeting = assistant.TEXTS["call_start"], assistant.TEXTS["voice_greeting"]
        system, channel = assistant.SYSTEM_PROMPTS["phone"], "phone"
    return {
        "history": [
            {"role": "user", "content": start},
            {"role": "assistant", "content": greeting},
        ],
        "system": system,
        "greeting": greeting,
        "channel": channel,
        "chat_id": chat_id,
        "username": username,
        "phone": phone,
    }

# ---------------------------------------------------------------------------
# Starting an outgoing call (from Telegram /call)
# ---------------------------------------------------------------------------


def start_outbound_call(phone, chat_id=None, username="", order=None):
    call = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls.create(
        to=phone,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_URL}/twiml",
    )
    sessions[call.sid] = new_session(chat_id, username, phone, order)
    return call.sid

# ---------------------------------------------------------------------------
# TwiML: connect the call to our WebSocket
# ---------------------------------------------------------------------------


def is_live_call(call_sid):
    if not call_sid.startswith("CA"):
        return False
    try:
        call = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls(call_sid).fetch()
    except Exception:
        return False
    return call.status in ("queued", "ringing", "in-progress")


@app.post("/twiml")
async def twiml(request: Request):
    form = dict(await request.form())
    signature = request.headers.get("X-Twilio-Signature", "")
    call_sid = form.get("CallSid", "")
    if not RequestValidator(TWILIO_AUTH_TOKEN).validate(f"{PUBLIC_URL}/twiml", form, signature):
        # some Twilio requests arrive without a signature: check the call really exists in our account
        if not await asyncio.to_thread(is_live_call, call_sid):
            print(f"[{call_sid}] Rejected /twiml request: bad signature and unknown call")
            return Response(status_code=403)

    if call_sid not in sessions:  # incoming call to our Twilio number
        sessions[call_sid] = new_session(phone=form.get("From", ""))

    response = VoiceResponse()
    connect = Connect(action=f"{PUBLIC_URL}/relay-end")
    connect.conversation_relay(
        url=f"{PUBLIC_URL.replace('https://', 'wss://')}/ws",
        welcomeGreeting=sessions[call_sid]["greeting"],
        language=LANGUAGE,
        ttsProvider="ElevenLabs",
        transcriptionProvider="Deepgram",
        speechModel="nova-3-general",
        interruptible="any",
        interruptSensitivity="medium",
        hints="Беррі Фарм, саджанці, лохина, гортензія, павловнія, ківі, ожина, фундук, Нова Пошта",
    )
    response.append(connect)
    return Response(content=str(response), media_type="text/xml")


@app.post("/relay-end")
async def relay_end(request: Request):
    """Twilio calls this when ConversationRelay ends - the only place its errors are reported."""
    form = dict(await request.form())
    details = {k: v for k, v in form.items() if k.startswith(("Session", "Error", "Handoff"))}
    print(f"[{form.get('CallSid')}] ConversationRelay ended: {details}")
    response = VoiceResponse()
    response.hangup()
    return Response(content=str(response), media_type="text/xml")

# ---------------------------------------------------------------------------
# WebSocket: the live conversation
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def conversation(websocket: WebSocket):
    await websocket.accept()
    call_sid, session, reply_task = None, None, None

    try:
        while True:
            data = json.loads(await websocket.receive_text())
            kind = data.get("type")

            if kind == "setup":
                call_sid = data.get("callSid")
                session = sessions.get(call_sid)
                if session is None:  # not a call we started or answered - reject
                    await websocket.close()
                    return
                print(f"[{call_sid}] Call connected")

            elif kind == "prompt" and data.get("last") and session:
                text = data["voicePrompt"]
                print(f"[{call_sid}] Client: {text}")
                if reply_task and not reply_task.done():
                    reply_task.cancel()
                add_user_message(session["history"], text)
                reply_task = asyncio.create_task(answer(websocket, call_sid, session))

            elif kind == "interrupt" and session:
                heard = data.get("utteranceUntilInterrupt", "")
                print(f"[{call_sid}] Interrupted after: '{heard}'")
                if reply_task and not reply_task.done():
                    reply_task.cancel()
                cut_last_answer(session["history"], heard)

            elif kind == "error":
                print(f"[{call_sid}] ConversationRelay error: {data.get('description')}")

    except WebSocketDisconnect:
        print(f"[{call_sid}] Call ended")
    finally:
        if reply_task and not reply_task.done():
            reply_task.cancel()
        await finish_call(call_sid)


def add_user_message(history, text):
    if history[-1]["role"] == "user":  # the previous answer was cancelled before it started
        history[-1]["content"] += " " + text
    else:
        history.append({"role": "user", "content": text})


def cut_last_answer(history, heard):
    """Keep only the part of the AI's answer the customer actually heard."""
    if history[-1]["role"] == "assistant":
        if heard.strip():
            history[-1]["content"] = heard.strip() + "…"
        else:
            history.pop()


async def send_text(websocket, token, last=False):
    await websocket.send_text(json.dumps({"type": "text", "token": token, "last": last}))


async def answer(websocket, call_sid, session):
    """Streams Claude's answer to Twilio while it is being written."""
    full, sent = "", 0
    try:
        async with assistant.async_client.beta.messages.stream(
            model=MODEL,
            max_tokens=4000,
            system=session["system"],
            messages=session["history"],
            output_config={"effort": "low"},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            async for chunk in stream.text_stream:
                full += chunk
                # never speak the [END_CALL] marker: hold back everything after "["
                safe = full.split("[", 1)[0]
                if len(safe) > sent:
                    await send_text(websocket, safe[sent:])
                    sent = len(safe)
            final = await stream.get_final_message()
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        print(f"[{call_sid}] Claude error: {e}")
        await send_text(websocket, assistant.TEXTS["voice_error"], last=True)
        return

    if final.stop_reason == "refusal" and not full:
        await send_text(websocket, assistant.TEXTS["voice_error"], last=True)
        return

    finished = assistant.END_MARKER in full
    text = full.replace(assistant.END_MARKER, "").rstrip()
    await send_text(websocket, text[sent:], last=True)
    session["history"].append({"role": "assistant", "content": text})
    print(f"[{call_sid}] AI: {text}")

    if finished:
        # let the goodbye be spoken, then end the conversation (TwiML then hangs up)
        await asyncio.sleep(len(text[sent:]) / CHARS_PER_SECOND + 1.5)
        await websocket.send_text(json.dumps({"type": "end"}))


async def finish_call(call_sid):
    session = sessions.pop(call_sid, None)
    if not session:
        return
    data = await asyncio.to_thread(
        assistant.finish_conversation, session["history"], session["channel"],
        chat_id=session["chat_id"], username=session["username"],
        caller_phone=session["phone"],
    )
    if data and session["chat_id"] and on_call_saved:
        await asyncio.to_thread(on_call_saved, session["chat_id"], data)


def run_server():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
