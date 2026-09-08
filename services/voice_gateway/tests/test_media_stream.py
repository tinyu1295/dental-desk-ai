import asyncio
import base64
import json
import math
import struct
import sys
import wave
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from audio import mulaw_to_pcm16, pcm16_to_mulaw

WS_URL = "ws://localhost:8001/media-stream"
SAMPLE_RATE = 8000
DURATION_SECONDS = 1
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160 samples/frame @ 8kHz


def generate_tone_pcm16(frequency=440, duration=DURATION_SECONDS, rate=SAMPLE_RATE):
    n_samples = int(rate * duration)
    samples = [
        int(32767 * 0.3 * math.sin(2 * math.pi * frequency * i / rate))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


async def run():
    pcm16 = generate_tone_pcm16()
    mulaw = pcm16_to_mulaw(pcm16)
    frames = [mulaw[i:i + FRAME_SAMPLES]
              for i in range(0, len(mulaw), FRAME_SAMPLES)]

    echoed_pcm16 = bytearray()

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(json.dumps({
            "event": "start",
            "start": {
                "streamSid": "MZtest1234567890",
                "callSid": "CAtest1234567890",
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": SAMPLE_RATE, "channels": 1},
            },
            "streamSid": "MZtest1234567890",
        }))

        async def receiver():
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("event") == "media":
                        payload = base64.b64decode(msg["media"]["payload"])
                        echoed_pcm16.extend(mulaw_to_pcm16(payload))
            except websockets.exceptions.ConnectionClosed:
                pass

        recv_task = asyncio.create_task(receiver())

        for frame in frames:
            await ws.send(json.dumps({
                "event": "media",
                "streamSid": "MZtest1234567890",
                "media": {"payload": base64.b64encode(frame).decode()},
            }))
            await asyncio.sleep(FRAME_MS / 1000)

        await ws.send(json.dumps({
            "event": "stop",
            "streamSid": "MZtest1234567890",
            "stop": {"callSid": "CAtest1234567890"},
        }))

        await asyncio.sleep(0.5)  # let any trailing echoed frames arrive
        recv_task.cancel()

    print(
        f"Sent {len(frames)} frames, received {len(echoed_pcm16) // 2} echoed samples")

    with wave.open("echo_test.wav", "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(bytes(echoed_pcm16))

    print("Wrote echo_test.wav")


if __name__ == "__main__":
    asyncio.run(run())
