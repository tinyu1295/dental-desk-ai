# Testing

Manual smoke tests for `voice_gateway`, run via the [Makefile](../../Makefile)
at the repository root. These hit the running HTTP API directly (real `curl`
requests) — they are not unit tests, they confirm the service actually
comes up and responds correctly.

## Prerequisites

Run these from the repository root (same directory as the `Makefile`), with
the stack up:

```bash
docker compose up -d --build
```

`--build` matters whenever `app/*.py` in this service changed since the
container was last built — Docker won't pick up code changes on its own,
and `/health` will still return `200` even when the container is serving a
stale build. If in doubt, always rebuild before testing.

Note the port: `voice_gateway` is mapped to host port **8001** (container
port 8000), not 8000 — see [docker-compose.yml](../../docker-compose.yml).
`booking_service` already owns 8000.

`make voice-media-stream` (below) additionally runs on your host rather than
in a container, so it needs local deps installed first:

```bash
python3 -m pip install websockets   # add audioop-lts too if your python3 --version is 3.13+
```

## Running the tests

```bash
make voice-health   # liveness check
```

`make voice-health` doesn't include the Twilio signature checks — those
need `TWILIO_AUTH_TOKEN` exported in your shell first (same value as
`.env`), so they're run explicitly:

```bash
export TWILIO_AUTH_TOKEN=your_auth_token_here   # same value as in .env
make voice-verify   # runs voice-verify-signature + voice-verify-status + voice-verify-reject
```

## Targets

### `make voice-health`

| Target | Checks |
|---|---|
| `make voice-health-live` | `GET /health` — is the process up at all? No dependency on Postgres or `booking_service` — per the comment in [app/main.py](app/main.py), that's intentional until this service actually talks to `booking_service`, at which point a `/health/ready` route and matching `voice-health-ready` target belong here, mirroring `booking_service`'s [health-ready](../booking_service/TESTING.md). |

### `make voice-verify`

**Requires `TWILIO_AUTH_TOKEN` exported first.** The container reads its
copy from [.env](../../.env) via `docker-compose.yml`;
[test_signature.py](tests/test_signature.py) reads its copy from your shell env
to compute a valid signature locally. The two have to agree, or the
signature it prints won't validate against the running container:

```bash
export TWILIO_AUTH_TOKEN=your_auth_token_here   # same value as in .env
```

| Target | Scenario | Expect |
|---|---|---|
| `make voice-verify-signature` | Computes a valid Twilio signature over a fixed set of fake form params and curls `/twilio/voice` with it directly | `200` with the `<Connect><Stream>` TwiML body: `<Response><Connect><Stream url="..." /></Connect></Response>` |
| `make voice-verify-status` | Same, but curls `/twilio/status` with `CallStatus=completed` | `200` with an empty body |
| `make voice-verify-reject` | Same request shape as `voice-verify-signature`, garbage `X-Twilio-Signature` header | `403` with an empty body |

`make voice-verify` runs all three, in order — no manual copy-paste needed
for any of them. (`services/voice_gateway/tests/test_signature.py --sig-only <url>
[callStatus]` is what `voice-verify-signature`/`voice-verify-status` call
under the hood to get just the bare signature — first arg overrides the
full URL the signature is computed over (e.g. `$(VOICE_HOST)/twilio/status`),
second arg optionally overrides `CallStatus`. Run it without `--sig-only` if
you want the human-readable dump plus a ready-to-copy curl command instead.)

### `make voice-media-stream`

Not a curl test — opens a real WebSocket to `/media-stream`
([test_media_stream.py](tests/test_media_stream.py)), sends a 1s 440Hz tone as
mu-law frames (mimicking Twilio's audio stream), and expects the server to
echo audio back. Runs on your host (not in a container) — see
Prerequisites above for the one-time `pip install`:

```bash
make voice-media-stream
```

Expect console output like `Sent 50 frames, received 8000 echoed samples`
and a `services/voice_gateway/echo_test.wav` written to disk — play it back
to confirm the echo is a clean tone, not noise or silence.

**Note:** unlike the curl-based `voice-*` targets above, the WebSocket URL
is hardcoded inside `test_media_stream.py` itself
(`ws://localhost:8001/media-stream`) — it does not read `$(VOICE_HOST)`, so
overriding `VOICE_HOST` has no effect on this target. Edit the `WS_URL`
constant in that file if you need to point it elsewhere.

### `make voice-bedrock-stream`

> **⚠️ Makes a real, billed call to AWS Bedrock.** Unlike every other target
> in this file, this one is not free and does not run against local/mocked
> infrastructure — it needs real AWS credentials with access to the Nova
> Sonic model, and every run incurs real usage cost. Don't run this as part
> of a routine/automated check.

Opens a real bidirectional stream to Bedrock Nova Sonic
([test_bedrock_stream.py](tests/test_bedrock_stream.py) →
[app/bedrock_stream.py](app/bedrock_stream.py)), sends audio, and prints
each event Bedrock streams back. By default it sends a synthetic 2s 440Hz
tone; pass a real recording instead with `WAV=`. Runs on your host, not the
container, and does **not** require the docker compose stack up — it talks
to Bedrock directly, not to `voice_gateway`.

One-time local deps:

```bash
cd services/voice_gateway
python3 -m pip install aws_sdk_bedrock_runtime smithy-aws-core
```

Then credentials, exported in the same shell you run `make` from. If your
org uses AWS SSO (the common case), don't hand-type long-lived keys — use
the AWS CLI's own bridge, built for exactly this.

**One-time per profile:** if `your-sso-profile` doesn't exist yet in
`~/.aws/config`, set it up first — this walks you through your org's SSO
start URL, region, account, and role, and only needs to be done once (not
before every login):

```bash
aws configure sso --profile your-sso-profile
```

**Every time credentials expire (see below):**

```bash
aws sso login --profile your-sso-profile   # opens the browser, authenticates
eval "$(aws configure export-credentials --profile your-sso-profile --format env)"
```

`export-credentials` prints `export AWS_ACCESS_KEY_ID=...` /
`AWS_SECRET_ACCESS_KEY=...` / `AWS_SESSION_TOKEN=...` for the resolved
temporary credentials, and `eval` applies them to your current shell —
`test_bedrock_stream.py` (via `smithy-aws-core`'s default credential chain)
picks all three up the same way it would pick up manually-exported keys.
Needs a reasonably recent AWS CLI v2 — `aws --version` to check if you're
unsure; `export-credentials` was added a couple years back.

**These expire.** SSO-vended session tokens are typically good for ~1 hour
(depends on your org's settings) — if `test_bedrock_stream.py` suddenly
starts failing auth after it was working, that's the first thing to
suspect. Just re-run the `eval` line to refresh.

Then, from the repository root:

```bash
make voice-bedrock-stream                       # synthetic 440Hz tone
make voice-bedrock-stream WAV=my_recording.wav  # your own recording instead
```

`WAV` is resolved relative to wherever you run `make` from (not relative to
`services/voice_gateway`), so a path like `my_recording.wav` sitting in the
repository root works as shown. The file must be **16kHz mono 16-bit PCM**
— `test_bedrock_stream.py` checks this upfront and fails fast with a
ready-to-run `ffmpeg` conversion command if it isn't.

**Recording your own test clip (macOS):** rather than converting an
existing file, you can record straight from your Mac's mic, already in the
exact format Bedrock expects, with `ffmpeg`'s `avfoundation` input:

```bash
ffmpeg -f avfoundation -i ":2" -ar 16000 -ac 1 -sample_fmt s16 -t 5 my_recording.wav
```

`:2` is the audio device index — it's specific to your machine and will
almost certainly not be `2` on yours. List your devices first to find the
right number:

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

Look under "AVFoundation audio devices" in the output for your mic (e.g.
built-in mic vs. an external interface) and use its index. `-t 5` caps the
recording at 5 seconds — drop or change it to record longer; `ffmpeg` will
otherwise keep recording until you press `q` or `Ctrl+C`.

Expect `EVENT: [...]` lines printed as Bedrock responds, ending near a
`completionEnd` event, then a `services/voice_gateway/bedrock_test_output.wav`
written to disk if any audio came back.

## Watching logs while testing

Each request logs its outcome (see [app/main.py](app/main.py)), so you can
watch requests land in real time while running the tests above:

```bash
docker compose logs -f voice_gateway
```

## Troubleshooting

- **A `/twilio/voice` or `/health` request doesn't respond at all** → check
  you're hitting port `8001`, not `8000` (that's `booking_service`).
- **A route 404s or returns something unexpected** → the container is
  running stale code. Rebuild: `docker compose up -d --build`.
- **The "valid signature" curl from `voice-verify-signature` or
  `voice-verify-status` still comes back `403`** → `TWILIO_AUTH_TOKEN` in
  your shell doesn't match what the container has. The container's copy
  comes from `.env` at the time it was last started — if `.env` changed
  since, restart it (`docker compose up -d voice_gateway`) to pick it up,
  and make sure the value you `export`ed matches exactly.
- **Testing a route other than `/twilio/voice` or `/twilio/status`
  manually** → pass its full URL as `test_signature.py`'s first arg, e.g.
  `python3 services/voice_gateway/tests/test_signature.py
  http://localhost:8001/twilio/status completed`. The second arg overrides
  `CallStatus` in the signed params.
- **`docker inspect <container> --format '{{.State.Health.Status}}'`** shows
  Docker's own periodic healthcheck result (`healthy` / `unhealthy`),
  independent of manually running `make voice-health` yourself.
- **`make voice-media-stream` hangs or refuses to connect** → it dials
  `ws://localhost:8001/media-stream` directly, so the container needs to be
  up on port `8001` first (`docker compose up -d --build`) — it isn't
  covered by `voice-health`.
- **`ModuleNotFoundError: No module named 'audioop'`** → your `python3` is
  3.13+, where `audioop` was dropped from the stdlib. Install the backport:
  `python3 -m pip install audioop-lts` (see Prerequisites above).
- **`make voice-bedrock-stream` fails with a credentials/auth error** →
  first check whether it *was* working earlier in the session — if so, your
  SSO session token likely just expired (~1 hour typical); re-run `eval
  "$(aws configure export-credentials --profile your-sso-profile --format
  env)"` (see above) to refresh it. If it's never worked, confirm
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` are
  actually exported in the shell you ran `make` from (`env | grep AWS_`),
  or that the underlying IAM principal doesn't have `bedrock:*` permission
  for the Nova Sonic model (`amazon.nova-sonic-v1:0`, `us-east-1` — see the
  `model_id`/`region` defaults in
  [app/bedrock_stream.py](app/bedrock_stream.py)). Confirm model access is
  enabled for your account in the Bedrock console for that region.
- **`make voice-bedrock-stream` prints "No audio received back."** → the
  events came back but produced no audio output — check the `EVENT:` lines
  printed beforehand for what Bedrock actually sent (e.g. an error/content
  event) rather than assuming the call itself failed silently.
- **`make voice-bedrock-stream WAV=...` raises a `ValueError` about
  Hz/ch/bit** → the file isn't 16kHz mono 16-bit PCM. The error message
  includes a ready-to-run `ffmpeg` command to convert it — see the `WAV=`
  section above.
- **`make voice-bedrock-stream WAV=...` says the file doesn't exist, even
  though it's sitting right there** → the path is resolved relative to
  wherever you ran `make` from (the repository root), not relative to
  `services/voice_gateway/`. Use a path from the root, or an absolute one.
