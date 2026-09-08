import logging
import os
import base64
import json
import asyncio
import time
from xml.sax.saxutils import escape as xml_escape

import requests

from dotenv import load_dotenv, find_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from twilio.request_validator import RequestValidator

from bedrock_stream import BedrockStreamManager
from audio import mulaw_to_pcm16, pcm16_to_mulaw, resample_pcm16

load_dotenv(find_dotenv())

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("voice_gateway")

app = FastAPI()


def _mask_phone_for_log(phone: str | None) -> str:
    """Masks a phone number for logging -- caller IDs are personal data
    and shouldn't sit in plaintext in log files that get pasted around.
    Only used at logger.info() call sites; the real, unmasked value keeps
    flowing through everywhere it's actually needed functionally (Twilio
    signature validation, the caller-ID phone offer feature)."""
    if not phone:
        return "(none)"
    return f"{phone[:3]}*****"

_validator = RequestValidator(os.environ.get("TWILIO_AUTH_TOKEN", ""))
MEDIA_STREAM_URL = os.environ.get(
    "MEDIA_STREAM_URL", "ws://localhost:8001/media-stream")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8001")

TWILIO_SAMPLE_RATE = 8000
NOVA_INPUT_SAMPLE_RATE = 16000
NOVA_OUTPUT_SAMPLE_RATE = 24000

BOOKING_SERVICE_URL = os.environ.get("BOOKING_SERVICE_URL", "http://booking_service:8000")

TOOL_ROUTES = {
    "findPatientTool": "/tools/find-patient",
    "registerPatientTool": "/tools/register-patient",
    "updatePatientPhoneTool": "/tools/update-patient-phone",
    "getDoctorTool": "/tools/get-doctor",
    "findNextOpeningTool": "/tools/find-next-opening",
    "bookAppointmentTool": "/tools/book-appointment",
}


def _tool_executor(tool_name: str, content_data: dict) -> dict:
    """Runs synchronously off the event loop (bedrock_stream.py's
    _execute_tool calls this via loop.run_in_executor) -- requests.post is
    blocking, same treatment boto3 calls got in the hotel project's version
    of this function."""
    route = TOOL_ROUTES.get(tool_name)
    if route is None:
        return {"error": f"Unsupported tool: {tool_name}"}

    try:
        response = requests.post(f"{BOOKING_SERVICE_URL}{route}", json=content_data, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error("tool_executor: booking_service call failed tool=%s error=%r", tool_name, e)
        return {"error": f"booking_service request failed: {e}"}


@app.get("/health")
def health():
    """Liveness check: is the process up and able to respond? No dependency
    on booking_service on purpose -- that check belongs on a /health/ready
    route once this service actually talks to it."""
    return {"status": "ok"}


def _validate_twilio_request(request: Request, form: dict) -> bool:
    """Recomputes Twilio's HMAC-SHA1 signature over the request URL + form
    params and compares it to the X-Twilio-Signature header. Uses
    PUBLIC_BASE_URL rather than str(request.url) -- behind ngrok, Uvicorn
    doesn't see the real public https:// scheme through the proxy (Docker's
    gateway IP isn't a trusted proxy by default), so request.url would
    report http:// even though Twilio signed the real https://...ngrok
    URL, and every real request would fail validation."""
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{PUBLIC_BASE_URL}{request.url.path}"
    return _validator.validate(url, form, signature)


@app.get("/greeting.wav")
def greeting_wav():
    """Pre-recorded, not generated live -- Nova Sonic can't be safely forced
    to speak first mid-call (a hidden role="USER" TEXT trigger breaks every
    subsequent turn with a ValidationException, per the hotel project's
    BUILD_LOG.md). Generated once, offline, via generate_greeting.py, in
    Nova Sonic's own voice; Twilio plays this static file before the live
    stream ever connects, so the caller hears something immediately
    instead of dead air while waiting for the model's first turn."""
    return FileResponse("audio/greeting.wav", media_type="audio/wav")


@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = dict(await request.form())
    if not _validate_twilio_request(request, form):
        logger.warning("twilio/voice: invalid signature, rejecting")
        return Response(status_code=403)

    logger.info("twilio/voice: call from=%s callSid=%s",
                _mask_phone_for_log(form.get("From")), form.get("CallSid"))

    # Proactive greeting (<Play> pointing at our self-hosted greeting.wav)
    # is reverted, not abandoned. Confirmed broken for this number's calls
    # THREE times, including the hotel project's exact, byte-identical
    # working code reapplied fresh - greeting.wav fetches successfully
    # every time, but /media-stream never gets a single connection
    # attempt, and the call just ends ~6-9s later. Not a code difference:
    # every call here carries X-Twilio-Service-Flow-Event with
    # call_transfer_url, meaning this number's Voice Configuration routes
    # through a Twilio Studio Flow "Connect Call To" transfer - the hotel
    # project's number may not. See DENTAL_LOG.md for the full comparison
    # table and the two unconfirmed leads (Console config, S3 hosting).
    # Plain <Connect><Stream> below is what's actually proven reliable
    # here (15+ calls) - the caller gets Nova Sonic's own live greeting
    # instead of a pre-recorded one.
    #
    # Passed through as a <Stream> Parameter so /media-stream's "start"
    # event carries it in customParameters -- this is the only way Twilio
    # gets the caller's number into the media WebSocket at all, since the
    # Media Streams protocol itself doesn't include From/To. Used as a
    # fallback contact number if manual phone-digit capture keeps failing
    # (see collectPhoneDigitsTool's escalation path in bedrock_stream.py).
    caller_number = xml_escape(form.get("From", ""))
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="{MEDIA_STREAM_URL}">'
        f'<Parameter name="callerNumber" value="{caller_number}" />'
        '</Stream></Connect></Response>'
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = dict(await request.form())
    if not _validate_twilio_request(request, form):
        logger.warning("twilio/status: invalid signature, rejecting")
        return Response(status_code=403)

    logger.info("twilio/status: callSid=%s callStatus=%s",
                form.get("CallSid"), form.get("CallStatus"))
    return Response(status_code=200)


END_CALL_MAX_WAIT_FOR_AUDIO_SECONDS = 5.0
END_CALL_GRACE_PERIOD_SECONDS = 1.0

FRAME_MS = 20
FRAME_BYTES = TWILIO_SAMPLE_RATE * FRAME_MS // 1000  # 160 bytes = 20ms of mu-law @ 8kHz


async def _clear_twilio_playback(websocket: WebSocket, stream_sid_ref: dict):
    """Tells Twilio to discard whatever audio it's already buffered but
    hasn't played yet -- the "clear" event in the Media Streams outbound
    protocol. Cheap and idempotent if there's nothing to clear, so this is
    safe to call unconditionally on every barge-in signal rather than
    trying to first determine whether anything was actually playing."""
    await websocket.send_text(json.dumps({
        "event": "clear",
        "streamSid": stream_sid_ref["value"],
    }))


async def _forward_nova_audio_to_twilio(websocket: WebSocket, manager: BedrockStreamManager, stream_sid_ref: dict):
    try:
        while True:
            pcm16_24k = await manager.audio_output_queue.get()
            pcm16_8k = resample_pcm16(pcm16_24k, NOVA_OUTPUT_SAMPLE_RATE, TWILIO_SAMPLE_RATE)
            mulaw_out = pcm16_to_mulaw(pcm16_8k)

            # Real-time-paced 20ms frames -- a whole burst at once (Nova
            # Sonic can hand back a full sentence in one chunk) overruns
            # Twilio's playback buffer and sounds like lagging/skipping.
            for i in range(0, len(mulaw_out), FRAME_BYTES):
                # Barge-in check between frames, not just once per chunk --
                # a chunk can be several seconds of audio at 20ms/frame, so
                # this is what actually makes the cutoff prompt rather than
                # waiting for the current chunk to finish sending first.
                if manager.barge_in_signal.is_set():
                    manager.barge_in_signal.clear()
                    while not manager.audio_output_queue.empty():
                        manager.audio_output_queue.get_nowait()
                    await _clear_twilio_playback(websocket, stream_sid_ref)
                    logger.info("media-stream: barge-in, cleared queued/buffered audio")
                    break
                chunk = mulaw_out[i:i + FRAME_BYTES]
                await websocket.send_text(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid_ref["value"],
                    "media": {"payload": base64.b64encode(chunk).decode()},
                }))
                await asyncio.sleep(FRAME_MS / 1000)
    except asyncio.CancelledError:
        pass


async def _hangup_after_end_call(websocket: WebSocket, manager: BedrockStreamManager):
    try:
        await manager.call_should_end.wait()

        # Wait for the farewell (triggered by endCallTool's own tool-result
        # instruction) to actually start arriving, then fully drain, before
        # hanging up -- a fixed sleep risks cutting it off mid-sentence or
        # hanging up before it starts.
        deadline = time.monotonic() + END_CALL_MAX_WAIT_FOR_AUDIO_SECONDS
        while manager.audio_output_queue.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        while not manager.audio_output_queue.empty():
            await asyncio.sleep(0.1)

        await asyncio.sleep(END_CALL_GRACE_PERIOD_SECONDS)
        await websocket.close()
    except asyncio.CancelledError:
        pass


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    stream_sid_ref = {"value": None}
    manager: BedrockStreamManager | None = None
    forward_task: asyncio.Task | None = None
    hangup_task: asyncio.Task | None = None
    frame_count = 0

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "connected":
                logger.info("media-stream: connected protocol=%s", msg.get("protocol"))

            elif event == "start":
                stream_sid_ref["value"] = msg["start"]["streamSid"]
                caller_number = msg["start"].get("customParameters", {}).get("callerNumber") or None
                logger.info("media-stream: start streamSid=%s callSid=%s callerNumber=%s",
                            stream_sid_ref["value"], msg["start"].get("callSid"),
                            _mask_phone_for_log(caller_number))
                manager = BedrockStreamManager(tool_executor=_tool_executor, caller_phone_number=caller_number)
                await manager.initialize_stream()
                await manager.send_audio_content_start_event()
                forward_task = asyncio.create_task(
                    _forward_nova_audio_to_twilio(websocket, manager, stream_sid_ref)
                )
                hangup_task = asyncio.create_task(_hangup_after_end_call(websocket, manager))

            elif event == "media" and manager is not None:
                mulaw_bytes = base64.b64decode(msg["media"]["payload"])
                pcm16_8k = mulaw_to_pcm16(mulaw_bytes)
                pcm16_16k = resample_pcm16(pcm16_8k, TWILIO_SAMPLE_RATE, NOVA_INPUT_SAMPLE_RATE)
                manager.add_audio_chunk(pcm16_16k)
                frame_count += 1

            elif event == "stop":
                logger.info("media-stream: stop streamSid=%s framesReceived=%d",
                            stream_sid_ref["value"], frame_count)
                break

    except WebSocketDisconnect:
        logger.info("media-stream: client disconnected streamSid=%s", stream_sid_ref["value"])
    finally:
        if forward_task is not None:
            forward_task.cancel()
        if hangup_task is not None:
            hangup_task.cancel()
        if manager is not None:
            await manager.close()
