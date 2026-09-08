import asyncio
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from bedrock_stream import BedrockStreamManager  # noqa: E402


def generate_tone_pcm16(frequency=440, duration=2, rate=16000):
    import math
    n_samples = int(rate * duration)
    samples = [
        int(32767 * 0.3 * math.sin(2 * math.pi * frequency * i / rate))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


def load_wav_pcm16(path, expected_rate=16000, expected_channels=1):
    with wave.open(path, "rb") as f:
        rate = f.getframerate()
        channels = f.getnchannels()
        sampwidth = f.getsampwidth()
        if rate != expected_rate or channels != expected_channels or sampwidth != 2:
            raise ValueError(
                f"{path} is {rate}Hz/{channels}ch/{sampwidth * 8}bit -- "
                f"expected {expected_rate}Hz/{expected_channels}ch/16bit. "
                f"Convert it first, e.g.: "
                f"ffmpeg -i {path} -ar {expected_rate} -ac {expected_channels} "
                f"-sample_fmt s16 converted.wav"
            )
        return f.readframes(f.getnframes())


def stub_tool_executor(tool_name, content_data):
    return {"error": "tool dispatch not wired up until Stage C"}


async def main():
    wav_path = sys.argv[1] if len(sys.argv) > 1 else None
    if wav_path:
        audio = load_wav_pcm16(wav_path)
        print(f"Loaded {wav_path} ({len(audio)} bytes of PCM16 audio)")
    else:
        audio = generate_tone_pcm16()
        print("No wav path given -- using synthetic tone")

    manager = BedrockStreamManager(tool_executor=stub_tool_executor)

    print("Connecting to Bedrock...")
    await manager.initialize_stream()
    print("Connected. Sending audio...")

    await manager.send_audio_content_start_event()

    # Stream in ~100ms chunks with real-time pacing, like a live mic feed,
    # instead of dumping the whole clip in one go.
    chunk_bytes = int(16000 * 0.1) * 2  # 100ms of 16kHz 16-bit mono
    for i in range(0, len(audio), chunk_bytes):
        manager.add_audio_chunk(audio[i:i + chunk_bytes])
        await asyncio.sleep(0.1)

    await asyncio.sleep(0.5)
    # No manual send_audio_content_end_event() here -- close() owns ending
    # the audio content block exclusively. Calling it here too double-ends
    # the same content_name, which Bedrock rejects with a ValidationException
    # ("No open content found for content name: ...").

    audio_out = bytearray()
    try:
        while True:
            event = await asyncio.wait_for(manager.output_queue.get(), timeout=8.0)
            keys = list(event.get("event", {}).keys())
            print(f"EVENT: {keys}")
            if "completionEnd" in event.get("event", {}):
                break
    except asyncio.TimeoutError:
        print("No more events (timeout) - wrapping up.")

    while not manager.audio_output_queue.empty():
        audio_out.extend(manager.audio_output_queue.get_nowait())

    await manager.close()

    if audio_out:
        with wave.open("bedrock_test_output.wav", "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(24000)
            f.writeframes(bytes(audio_out))
        print(f"Wrote bedrock_test_output.wav ({len(audio_out)} bytes)")
    else:
        print("No audio received back.")


if __name__ == "__main__":
    asyncio.run(main())
