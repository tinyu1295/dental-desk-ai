"""Audio conversion helpers -- Twilio Media Streams sends/expects mu-law
@ 8kHz; Bedrock's Nova Sonic needs PCM16 @ 16kHz in /
24kHz out.

Built on stdlib `audioop` -- zero extra dependencies inside the container
(python:3.12-slim still has it natively). `audioop` was deprecated in 3.11
and removed entirely in 3.13+, so if you run a script that imports this
module directly on your *host* machine (not inside the container) and your
local `python3` is 3.13+, install the backport first:
`python3 -m pip install audioop-lts`
"""
try:
    import audioop
except ImportError:  # host python3 is 3.13+; container stays on 3.12-slim
    import audioop_lts as audioop


def mulaw_to_pcm16(mulaw_bytes: bytes) -> bytes:
    """mu-law (1 byte/sample) -> linear PCM16 (2 bytes/sample)."""
    return audioop.ulaw2lin(mulaw_bytes, 2)


def pcm16_to_mulaw(pcm16_bytes: bytes) -> bytes:
    """Linear PCM16 (2 bytes/sample) -> mu-law (1 byte/sample)."""
    return audioop.lin2ulaw(pcm16_bytes, 2)


def resample_pcm16(pcm16_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample linear PCM16 mono audio between sample rates. `state=None`
    each call is intentional: call this once per audio chunk, not
    a continuous stream needing carried-over filter state between calls."""
    converted, _ = audioop.ratecv(pcm16_bytes, 2, 1, from_rate, to_rate, None)
    return converted
