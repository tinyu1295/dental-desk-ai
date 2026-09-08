.PHONY: test test-find-patient test-find-patient-existing test-find-patient-case-insensitive test-find-patient-no-match \
        test-register-patient test-register-patient-new test-register-patient-duplicate \
        test-update-patient-phone test-update-patient-phone-existing test-update-patient-phone-unknown \
        test-get-doctor test-get-doctor-existing test-get-doctor-unknown \
        test-find-next-opening test-find-next-opening-specific-doctor test-find-next-opening-skips-non-working-day \
        test-find-next-opening-no-opening test-find-next-opening-any-doctor test-find-next-opening-unknown-doctor \
        test-find-next-opening-invalid-duration \
        test-book-appointment test-book-appointment-fresh-slot test-book-appointment-exact-collision \
        test-book-appointment-partial-overlap test-book-appointment-back-to-back \
        test-book-appointment-different-doctor test-book-appointment-patient-overlap \
        test-book-appointment-unknown-doctor test-book-appointment-unknown-patient \
        test-book-appointment-outside-hours test-book-appointment-invalid-date \
        seed-db \
        health health-live health-ready \
        voice-health voice-health-live \
        voice-verify voice-verify-signature voice-verify-status voice-verify-reject \
        voice-media-stream voice-bedrock-stream

HOST ?= http://localhost:8000
VOICE_HOST ?= http://localhost:8001
# Optional path to a WAV file for `make voice-bedrock-stream WAV=my_recording.wav`
# -- must be 16kHz mono 16-bit PCM (see TESTING.md). Leave unset to use the
# built-in synthetic tone instead.
WAV ?=

## Run every manual test below, in order. Does NOT include test-book-appointment --
## that group writes real bookings and isn't safe to repeat without `make seed-db`
## first (see its section below), so it's run explicitly, not bundled into `test`.
test: test-find-patient test-register-patient test-update-patient-phone test-get-doctor test-find-next-opening

## Reset the DB to the seed fixtures in app/db.py -- run this before
## test-book-appointment (and before re-running it), since that group writes
## real appointments and drifts from what its own scenarios expect otherwise.
seed-db:
	@echo "== Resetting DB to seed fixtures =="
	docker compose exec booking_service python db.py

## ---------- health ----------
health: health-live health-ready

health-live:
	@echo "== health: liveness (process up, no DB dependency) =="
	curl -s -w '\n%{http_code}\n' $(HOST)/health

health-ready:
	@echo "== health: readiness (DB connection check) =="
	curl -s -w '\n%{http_code}\n' $(HOST)/health/ready

## ---------- voice_gateway health ----------
## No health-ready target yet -- voice_gateway doesn't talk to booking_service
## or Postgres yet (see app/main.py), so there's nothing for a readiness
## check to verify beyond liveness. Add one once that wiring lands.
voice-health: voice-health-live

voice-health-live:
	@echo "== voice_gateway health: liveness (process up, no dependencies) =="
	curl -s -w '\n%{http_code}\n' $(VOICE_HOST)/health

## ---------- voice_gateway: Twilio signature verification ----------
## Manual verification for POST /twilio/voice and /twilio/status's signature
## check. Assumes the stack is already up (see TESTING.md's Prerequisites).
## Requires TWILIO_AUTH_TOKEN exported in *this shell* (same value as .env --
## the container reads it from .env via docker-compose.yml, test_signature.py
## reads it from your shell env, so the two have to agree or the computed
## signature won't match what the container expects):
##
##   export TWILIO_AUTH_TOKEN=your_auth_token_here
##
## voice-verify runs all three: voice-verify-signature computes a valid
## signature and curls /twilio/voice with it directly -- expect 200 with the
## <Connect><Stream> TwiML body. voice-verify-status does the same against
## /twilio/status with CallStatus=completed -- expect 200 with an empty body.
## voice-verify-reject confirms a garbage signature is rejected. None need a
## manual step.
voice-verify: voice-verify-signature voice-verify-status voice-verify-reject

voice-verify-signature:
	@echo "== voice_gateway: computing a valid Twilio signature and calling /twilio/voice =="
	@echo "Requires TWILIO_AUTH_TOKEN exported in this shell (same value as .env)"
	@echo 'Expect: 200 with the <Connect><Stream> TwiML body.'
	@SIG=$$(python3 services/voice_gateway/tests/test_signature.py --sig-only $(VOICE_HOST)/twilio/voice) && \
	curl -s -w '\n%{http_code}\n' -X POST $(VOICE_HOST)/twilio/voice \
		-H "X-Twilio-Signature: $$SIG" \
		--data-urlencode "CallSid=CAtest1234567890" \
		--data-urlencode "From=+15551234567" \
		--data-urlencode "To=+15557654321" \
		--data-urlencode "CallStatus=ringing"
	@echo

voice-verify-status:
	@echo "== voice_gateway: computing a valid Twilio signature and calling /twilio/status =="
	@echo "Requires TWILIO_AUTH_TOKEN exported in this shell (same value as .env)"
	@echo 'Expect: 200 with an empty body.'
	@SIG=$$(python3 services/voice_gateway/tests/test_signature.py --sig-only $(VOICE_HOST)/twilio/status completed) && \
	curl -s -w '\n%{http_code}\n' -X POST $(VOICE_HOST)/twilio/status \
		-H "X-Twilio-Signature: $$SIG" \
		--data-urlencode "CallSid=CAtest1234567890" \
		--data-urlencode "From=+15551234567" \
		--data-urlencode "To=+15557654321" \
		--data-urlencode "CallStatus=completed"
	@echo

voice-verify-reject:
	@echo "== voice_gateway: garbage signature is rejected =="
	@echo 'Expect: 403 with an empty body'
	curl -s -w '\n%{http_code}\n' -X POST $(VOICE_HOST)/twilio/voice \
		-H "X-Twilio-Signature: not-a-real-signature" \
		--data-urlencode "CallSid=CAtest1234567890" \
		--data-urlencode "From=+15551234567" \
		--data-urlencode "To=+15557654321" \
		--data-urlencode "CallStatus=ringing"
	@echo

## ---------- voice_gateway: media-stream echo test ----------
## Not a curl test -- opens a real WebSocket to /media-stream, sends a 1s
## 440Hz tone as mu-law frames (mimicking Twilio's audio stream), and expects
## the server to echo audio back. Runs on your host, not in a container, so
## it needs local deps installed first -- see TESTING.md's Prerequisites
## (pip install websockets, plus audioop-lts if python3 is 3.13+). Requires
## the stack up first (docker compose up -d --build).
##
## NOTE: unlike the curl-based voice-* targets above, the WS URL is hardcoded
## in test_media_stream.py itself (ws://localhost:8001/media-stream) -- it
## does NOT read $(VOICE_HOST), so overriding VOICE_HOST won't affect this
## target. Edit the WS_URL constant in that file if you need to point it
## elsewhere.
voice-media-stream:
	@echo "== voice_gateway: media-stream echo test =="
	@echo "Expect: \"Sent N frames, received M echoed samples\" (M close to N's sample count) and a services/voice_gateway/echo_test.wav written to disk"
	cd services/voice_gateway && python3 tests/test_media_stream.py

## ---------- voice_gateway: Bedrock Nova Sonic stream ----------
## WARNING: makes a REAL, BILLED call to AWS Bedrock (Nova Sonic) -- not a
## free/local test. Needs real AWS credentials with Nova Sonic access, and
## runs on your host, not the container, so it needs local deps installed
## first -- see TESTING.md's Prerequisites (pip install
## aws_sdk_bedrock_runtime smithy-aws-core, plus AWS_ACCESS_KEY_ID /
## AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN exported in this shell -- via
## `aws sso login` + `aws configure export-credentials` if your org uses
## SSO, see TESTING.md). Does NOT require the docker compose stack up -- it
## talks to Bedrock directly, not to the voice_gateway container.
##
## Sends a synthetic 440Hz tone by default. Pass WAV=path/to/file.wav (path
## resolved relative to wherever you ran `make` from) to send real recorded
## audio instead -- it must be 16kHz mono 16-bit PCM, e.g.:
##   ffmpeg -i in.mp3 -ar 16000 -ac 1 -sample_fmt s16 my_recording.wav
voice-bedrock-stream:
	@echo "== voice_gateway: Bedrock Nova Sonic stream test =="
	@echo "WARNING: this makes a real, billed call to AWS Bedrock."
	@echo "Requires AWS credentials exported in this shell -- AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN (see TESTING.md)."
	@if [ -n "$(WAV)" ]; then echo "Using input WAV: $(WAV) (must be 16kHz mono 16-bit PCM)"; else echo "No WAV= given -- using the built-in synthetic 440Hz tone"; fi
	@echo "Expect: EVENT lines as Bedrock responds, ending near completionEnd, and a services/voice_gateway/bedrock_test_output.wav written to disk"
	cd services/voice_gateway && python3 tests/test_bedrock_stream.py $(if $(WAV),$(abspath $(WAV)))

## ---------- find-patient ----------
test-find-patient: test-find-patient-existing test-find-patient-case-insensitive test-find-patient-no-match

test-find-patient-existing:
	@echo "== find-patient: existing patient =="
	@echo 'Expect: {"found":true,"patientId":"P-1",...,"returningPatient":true,"usualDoctorId":"D-100","lastVisitDate":"2026-08-04"}'
	curl -s -w '\n' -X POST $(HOST)/tools/find-patient -H "Content-Type: application/json" \
		-d '{"firstName":"Anna","lastName":"Smith","dob":"1991-06-05"}'
	@echo

test-find-patient-case-insensitive:
	@echo "== find-patient: case-insensitive match =="
	@echo "Expect: same result as 'existing patient'"
	curl -s -w '\n' -X POST $(HOST)/tools/find-patient -H "Content-Type: application/json" \
		-d '{"firstName":"ANNA","lastName":"smith","dob":"1991-06-05"}'
	@echo

test-find-patient-no-match:
	@echo "== find-patient: no match =="
	@echo 'Expect: {"found":false}'
	curl -s -w '\n' -X POST $(HOST)/tools/find-patient -H "Content-Type: application/json" \
		-d '{"firstName":"Nobody","lastName":"Here","dob":"2000-01-01"}'
	@echo

## ---------- register-patient ----------
test-register-patient: test-register-patient-new test-register-patient-duplicate

test-register-patient-new:
	@echo "== register-patient: new patient =="
	@echo 'Expect: {"registered":true,"patientId":"P-..."}'
	curl -s -w '\n' -X POST $(HOST)/tools/register-patient -H "Content-Type: application/json" \
		-d '{"firstName":"John","lastName":"Doe","dob":"1998-03-12","phone":"+15550001111"}'
	@echo

test-register-patient-duplicate:
	@echo "== register-patient: exact duplicate =="
	@echo 'Expect: {"registered":false,"reason":"A patient with this name and date of birth already exists."}'
	curl -s -w '\n' -X POST $(HOST)/tools/register-patient -H "Content-Type: application/json" \
		-d '{"firstName":"Anna","lastName":"Smith","dob":"1991-06-05","phone":"+19995550000"}'
	@echo

## ---------- update-patient-phone ----------
test-update-patient-phone: test-update-patient-phone-existing test-update-patient-phone-unknown

test-update-patient-phone-existing:
	@echo "== update-patient-phone: existing patient =="
	@echo 'Expect: {"updated":true,"patientId":"P-1","phone":"+15559990000"}'
	curl -s -w '\n' -X POST $(HOST)/tools/update-patient-phone -H "Content-Type: application/json" \
		-d '{"patientId":"P-1","phone":"+15559990000"}'
	@echo

test-update-patient-phone-unknown:
	@echo "== update-patient-phone: unknown patient =="
	@echo 'Expect: {"updated":false,"reason":"No patient found with that ID."}'
	curl -s -w '\n' -X POST $(HOST)/tools/update-patient-phone -H "Content-Type: application/json" \
		-d '{"patientId":"P-999-DOES-NOT-EXIST","phone":"+15550000000"}'
	@echo

## ---------- get-doctor ----------
test-get-doctor: test-get-doctor-existing test-get-doctor-unknown

test-get-doctor-existing:
	@echo "== get-doctor: existing doctor =="
	@echo 'Expect: {"found":true,"doctorId":"D-100","name":"Dr. Elena Cruz"}'
	curl -s -w '\n' -X POST $(HOST)/tools/get-doctor -H "Content-Type: application/json" \
		-d '{"doctorId":"D-100"}'
	@echo
	@echo 'Expect: {"found":true,"doctorId":"D-200","name":"Dr. Marcus Lee"}'
	curl -s -w '\n' -X POST $(HOST)/tools/get-doctor -H "Content-Type: application/json" \
		-d '{"doctorId":"D-200"}'
	@echo

test-get-doctor-unknown:
	@echo "== get-doctor: unknown doctor =="
	@echo 'Expect: {"found":false} — a clean 200, not a 500'
	curl -s -w '\n' -X POST $(HOST)/tools/get-doctor -H "Content-Type: application/json" \
		-d '{"doctorId":"D-999-DOES-NOT-EXIST"}'
	@echo

## ---------- find-next-opening ----------
## Seed data: D-100 (Dr. Cruz) works Mon/Tue/Thu 09:00-17:00, Wed 10:00-18:00,
## Fri 09:00-15:00, off weekends, plus a training day 2026-08-15.
## D-200 (Dr. Lee) works Mon/Wed/Fri 08:00-14:00 only.
## Nothing's booked in a fresh seed, so these all land on the doctor's opening time.
test-find-next-opening: test-find-next-opening-specific-doctor test-find-next-opening-skips-non-working-day \
        test-find-next-opening-no-opening test-find-next-opening-any-doctor \
        test-find-next-opening-unknown-doctor test-find-next-opening-invalid-duration

test-find-next-opening-specific-doctor:
	@echo "== find-next-opening: one doctor, normal working day (Tuesday) =="
	@echo 'Expect: {"found":true,"date":"2026-08-11","startTime":"09:00","endTime":"09:30","doctorId":"D-100","doctorName":"Dr. Elena Cruz"}'
	curl -s -w '\n' -X POST $(HOST)/tools/find-next-opening -H "Content-Type: application/json" \
		-d '{"dateFrom":"2026-08-11","dateTo":"2026-08-11","doctorId":"D-100"}'
	@echo

test-find-next-opening-skips-non-working-day:
	@echo "== find-next-opening: same doctor, range starting on a day off (Saturday) =="
	@echo 'Expect: {"found":true,"date":"2026-08-10",...} — skips Sat/Sun, lands on Monday 2026-08-10 at 09:00'
	curl -s -w '\n' -X POST $(HOST)/tools/find-next-opening -H "Content-Type: application/json" \
		-d '{"dateFrom":"2026-08-08","dateTo":"2026-08-12","doctorId":"D-100"}'
	@echo

test-find-next-opening-no-opening:
	@echo "== find-next-opening: narrow window covering only non-working days =="
	@echo 'Expect: {"found":false,"reason":"No opening between 2026-08-08 and 2026-08-08."}'
	curl -s -w '\n' -X POST $(HOST)/tools/find-next-opening -H "Content-Type: application/json" \
		-d '{"dateFrom":"2026-08-08","dateTo":"2026-08-08","doctorId":"D-100"}'
	@echo

test-find-next-opening-any-doctor:
	@echo "== find-next-opening: no doctor specified, searches every doctor =="
	@echo 'Expect: {"found":true,"date":"2026-08-11","startTime":"08:00","doctorId":"D-200","doctorName":"Dr. Marcus Lee",...} — Dr. Lee opens at 08:00, earlier than Dr. Cruz'\''s 09:00, even though D-100 sorts first alphabetically'
	curl -s -w '\n' -X POST $(HOST)/tools/find-next-opening -H "Content-Type: application/json" \
		-d '{"dateFrom":"2026-08-11","dateTo":"2026-08-11"}'
	@echo

test-find-next-opening-unknown-doctor:
	@echo "== find-next-opening: unknown doctor =="
	@echo 'Expect: {"found":false,"reason":"No doctor found with that ID."}'
	curl -s -w '\n' -X POST $(HOST)/tools/find-next-opening -H "Content-Type: application/json" \
		-d '{"dateFrom":"2026-08-11","dateTo":"2026-08-11","doctorId":"D-999-DOES-NOT-EXIST"}'
	@echo

test-find-next-opening-invalid-duration:
	@echo "== find-next-opening: invalid duration, rejected before app code runs =="
	@echo "Expect: HTTP 422 (Unprocessable Entity) — Pydantic's Field(gt=0) validation, not a normal found:false response"
	curl -s -w "\nHTTP %{http_code}\n" -X POST $(HOST)/tools/find-next-opening -H "Content-Type: application/json" \
		-d '{"dateFrom":"2026-08-11","dateTo":"2026-08-11","doctorId":"D-100","durationMinutes":0}'
	@echo

## ---------- book-appointment ----------
## STATEFUL: each target below writes (or attempts to write) a real row to
## appointments, and later targets depend on earlier ones' bookings existing
## (e.g. the collision/overlap scenarios need test-book-appointment-fresh-slot's
## booking to already be there). Always run the full group in order via
## `make test-book-appointment`, not individual targets in isolation, and run
## `make seed-db` first -- re-running this group without reseeding will get
## different results the second time (a "fresh slot" booking will now collide).
test-book-appointment: test-book-appointment-fresh-slot test-book-appointment-exact-collision \
        test-book-appointment-partial-overlap test-book-appointment-back-to-back \
        test-book-appointment-different-doctor test-book-appointment-patient-overlap \
        test-book-appointment-unknown-doctor test-book-appointment-unknown-patient \
        test-book-appointment-outside-hours test-book-appointment-invalid-date

test-book-appointment-fresh-slot:
	@echo "== book-appointment: book a fresh slot =="
	@echo 'Expect: booked:true, plus doctorName:"Dr. Elena Cruz"'
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-1","doctorId":"D-100","date":"2026-08-11","startTime":"09:00","durationMinutes":30,"appointmentType":"CheckUp","reason":"E1 base booking"}'
	@echo

test-book-appointment-exact-collision:
	@echo "== book-appointment: exact same slot again, same doctor =="
	@echo 'Expect: {"booked":false,"reason":"That time overlaps an existing appointment for this doctor. Please choose another."}'
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-2","doctorId":"D-100","date":"2026-08-11","startTime":"09:00","durationMinutes":30,"appointmentType":"CheckUp","reason":"E2 exact collision"}'
	@echo

test-book-appointment-partial-overlap:
	@echo "== book-appointment: partial overlap (09:15-09:45 vs 09:00-09:30) =="
	@echo "Expect: booked:false, same doctor-overlap reason"
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-2","doctorId":"D-100","date":"2026-08-11","startTime":"09:15","durationMinutes":30,"appointmentType":"CheckUp","reason":"E3 partial overlap"}'
	@echo

test-book-appointment-back-to-back:
	@echo "== book-appointment: back-to-back, starts exactly when the first ends =="
	@echo "Expect: booked:true — touching ranges don't overlap"
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-2","doctorId":"D-100","date":"2026-08-11","startTime":"09:30","durationMinutes":30,"appointmentType":"CheckUp","reason":"E4 back-to-back"}'
	@echo

test-book-appointment-different-doctor:
	@echo "== book-appointment: same slot, different doctor -- doctor-overlap is per-doctor, not global =="
	@echo "Expect: both booked:true"
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-1","doctorId":"D-100","date":"2026-08-10","startTime":"09:00","durationMinutes":30,"appointmentType":"CheckUp","reason":"E5a"}'
	@echo
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-2","doctorId":"D-200","date":"2026-08-10","startTime":"09:00","durationMinutes":30,"appointmentType":"CheckUp","reason":"E5b different doctor"}'
	@echo

test-book-appointment-patient-overlap:
	@echo "== book-appointment: same patient (P-1, just booked D-100 above), different doctor, overlapping time =="
	@echo 'Expect: {"booked":false,"reason":"This patient already has another appointment at that time."} -- excl_patient_overlap'
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-1","doctorId":"D-200","date":"2026-08-10","startTime":"09:15","durationMinutes":30,"appointmentType":"CheckUp","reason":"E6 patient overlap"}'
	@echo

test-book-appointment-unknown-doctor:
	@echo "== book-appointment: unknown doctor =="
	@echo 'Expect: {"booked":false,"reason":"No doctor found with that ID."}'
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-1","doctorId":"D-999-DOES-NOT-EXIST","date":"2026-08-11","startTime":"11:00","durationMinutes":30,"appointmentType":"CheckUp","reason":"bad doctor"}'
	@echo

test-book-appointment-unknown-patient:
	@echo "== book-appointment: unknown patient =="
	@echo 'Expect: {"booked":false,"reason":"No patient found with that ID."}'
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-999-DOES-NOT-EXIST","doctorId":"D-100","date":"2026-08-11","startTime":"11:00","durationMinutes":30,"appointmentType":"CheckUp","reason":"bad patient"}'
	@echo

test-book-appointment-outside-hours:
	@echo "== book-appointment: requested time outside doctor's working hours =="
	@echo 'Expect: {"booked":false,"reason":"Requested time falls outside the doctor'\''s working hours."} (D-200 works 08:00-14:00 only)'
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-1","doctorId":"D-200","date":"2026-08-10","startTime":"22:00","durationMinutes":30,"appointmentType":"CheckUp","reason":"outside hours"}'
	@echo

test-book-appointment-invalid-date:
	@echo "== book-appointment: malformed date =="
	@echo 'Expect: {"booked":false,"reason":"Invalid date -- expected YYYY-MM-DD."}'
	curl -s -w '\n' -X POST $(HOST)/tools/book-appointment -H "Content-Type: application/json" \
		-d '{"patientId":"P-1","doctorId":"D-100","date":"not-a-date","startTime":"09:00","durationMinutes":30,"appointmentType":"CheckUp","reason":"bad date"}'
	@echo
