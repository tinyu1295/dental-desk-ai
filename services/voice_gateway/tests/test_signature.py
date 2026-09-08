import base64
import hashlib
import hmac
import os
import sys


def compute_signature(auth_token: str, url: str, params: dict) -> str:
    """Twilio's algorithm: URL + each POST param's key+value (no separator),
    sorted by key, concatenated onto the URL; HMAC-SHA1 with the auth token
    as key; base64-encode the digest."""
    data = url
    for key in sorted(params):
        data += key + params[key]
    digest = hmac.new(auth_token.encode(), data.encode(),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


DEFAULT_URL = "http://localhost:8001/twilio/voice"

if __name__ == "__main__":
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not auth_token:
        print("Set TWILIO_AUTH_TOKEN in your environment first.", file=sys.stderr)
        sys.exit(1)

    # --sig-only: print just the bare signature (used by `make
    # voice-verify-signature`/`voice-verify-status` to build and run the
    # curl itself) instead of the human-readable dump below. First
    # positional arg overrides the full URL the signature is computed over
    # -- keep it in sync with whatever the caller will actually curl (e.g.
    # Make's $(VOICE_HOST)/twilio/status), since Twilio's signature covers
    # the exact URL requested. Second positional arg overrides CallStatus,
    # which matters for /twilio/status (e.g. "completed", "no-answer") but
    # is harmless noise on /twilio/voice.
    sig_only = "--sig-only" in sys.argv[1:]
    positional = [a for a in sys.argv[1:] if a != "--sig-only"]
    url = positional[0] if positional else DEFAULT_URL
    call_status = positional[1] if len(positional) > 1 else "ringing"

    params = {
        "CallSid": "CAtest1234567890",
        "From": "+15551234567",
        "To": "+15557654321",
        "CallStatus": call_status,
    }

    signature = compute_signature(auth_token, url, params)

    if sig_only:
        print(signature)
        sys.exit(0)
