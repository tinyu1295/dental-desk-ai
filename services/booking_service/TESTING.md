# Testing

Manual smoke tests for `booking_service`, run via the [Makefile](../../Makefile)
at the repository root. These hit the running HTTP API directly (real `curl`
requests against a real DB) — they are not unit tests, they confirm the whole
stack (app + Postgres) actually works end to end.

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

## Running the tests

```bash
make test          # runs every scenario below, in order
make health         # liveness + readiness checks only
```

`make test` does **not** include `test-book-appointment` — that group writes
real appointments and isn't safe to repeat without resetting the DB first.
Run it explicitly, after reseeding:

```bash
make seed-db                # resets DB to app/db.py's fixtures
make test-book-appointment  # then run the booking scenarios
```

You can point at a different host (e.g. staging) by overriding `HOST`:

```bash
make test HOST=http://staging.example.com:8000
```

Each target prints what it expects, then the actual response, so you compare
by eye — there's no pass/fail assertion, just a clear diff to eyeball.

## Targets

### `make health`

| Target | Checks |
|---|---|
| `make health-live` | `GET /health` — is the process up at all? No DB call. |
| `make health-ready` | `GET /health/ready` — can the app reach Postgres (`SELECT 1`)? |

`health-live` returning `200` while `health-ready` returns `503` means the
app is up but can't reach the database — check the `db` container and
`DATABASE_URL`.

### `make test-find-patient`

| Target | Scenario | Expect |
|---|---|---|
| `test-find-patient-existing` | Known patient, exact-case name | `found:true` with full profile, `returningPatient`, `usualDoctorId`, `lastVisitDate` |
| `test-find-patient-case-insensitive` | Same patient, name in different case | Identical result to the above — confirms lookup is case-insensitive |
| `test-find-patient-no-match` | Name/DOB with no matching row | `{"found":false}` |

### `make test-register-patient`

| Target | Scenario | Expect |
|---|---|---|
| `test-register-patient-new` | Brand-new name + DOB | `{"registered":true,"patientId":"P-..."}` |
| `test-register-patient-duplicate` | Same name + DOB as an existing patient | `{"registered":false,"reason":"A patient with this name and date of birth already exists."}` |

### `make test-update-patient-phone`

| Target | Scenario | Expect |
|---|---|---|
| `test-update-patient-phone-existing` | Real `patientId` | `{"updated":true,"patientId":"...","phone":"..."}` |
| `test-update-patient-phone-unknown` | `patientId` that doesn't exist | `{"updated":false,"reason":"No patient found with that ID."}` |

### `make test-get-doctor`

| Target | Scenario | Expect |
|---|---|---|
| `test-get-doctor-existing` | Known `doctorId`s (`D-100`, `D-200`) | `{"found":true,"doctorId":"D-100","name":"Dr. Elena Cruz"}` and `{"found":true,"doctorId":"D-200","name":"Dr. Marcus Lee"}` |
| `test-get-doctor-unknown` | `doctorId` that doesn't exist | `{"found":false}` — a clean 200, not a 500 |

### `make test-find-next-opening`

Seed data: `D-100` (Dr. Cruz) works Mon/Tue/Thu 09:00–17:00, Wed 10:00–18:00,
Fri 09:00–15:00, off entirely on weekends, plus a training day on
2026-08-15. `D-200` (Dr. Lee) works Mon/Wed/Fri 08:00–14:00 only. Nothing's
booked yet in a fresh seed, so these all land on the doctor's opening time
as the earliest slot.

| Target | Scenario | Expect |
|---|---|---|
| `test-find-next-opening-specific-doctor` | One doctor (`D-100`), normal working day (Tuesday) | `{"found":true,"date":"2026-08-11","startTime":"09:00","endTime":"09:30","doctorId":"D-100","doctorName":"Dr. Elena Cruz"}` |
| `test-find-next-opening-skips-non-working-day` | Same doctor, range starting on a day off (Saturday) | `{"found":true,"date":"2026-08-10",...}` — skips Saturday (doesn't work) and Sunday, lands on Monday 2026-08-10 at 09:00 |
| `test-find-next-opening-no-opening` | Narrow window covering only a non-working day | `{"found":false,"reason":"No opening between 2026-08-08 and 2026-08-08."}` |
| `test-find-next-opening-any-doctor` | No `doctorId` — searches every doctor | `{"found":true,"date":"2026-08-11","startTime":"08:00","doctorId":"D-200","doctorName":"Dr. Marcus Lee",...}` — Dr. Lee opens at 08:00, earlier than Dr. Cruz's 09:00, so he wins even though `D-100` sorts first alphabetically |
| `test-find-next-opening-unknown-doctor` | `doctorId` that doesn't exist | `{"found":false,"reason":"No doctor found with that ID."}` |
| `test-find-next-opening-invalid-duration` | `durationMinutes:0` | HTTP `422` (Unprocessable Entity), not a normal `found:false` response — Pydantic's `Field(gt=0)` validation rejects it before the route body ever runs |

### `make test-book-appointment`

**Stateful — reset first.** Unlike the groups above, this one writes real
rows to `appointments`, and several scenarios depend on an earlier
scenario's booking already existing (the collision/overlap checks need
`test-book-appointment-fresh-slot`'s appointment to be there first). Always
run the whole group in order, and reseed before running it (or re-running
it) so the results below actually hold:

```bash
make seed-db                # resets DB to app/db.py's fixtures
make test-book-appointment  # runs all 10 scenarios below, in order
```

Re-running `test-book-appointment` without `make seed-db` in between will
diverge from the expected results — e.g. `test-book-appointment-fresh-slot`
would hit a collision against its own previous run's booking instead of
succeeding.

| Target | Scenario | Expect |
|---|---|---|
| `test-book-appointment-fresh-slot` | `P-1` books `D-100`, 2026-08-11 09:00 (E1, base booking) | `booked:true`, plus `doctorName:"Dr. Elena Cruz"` |
| `test-book-appointment-exact-collision` | `P-2` books the exact same slot as above, same doctor | `{"booked":false,"reason":"That time overlaps an existing appointment for this doctor. Please choose another."}` |
| `test-book-appointment-partial-overlap` | `P-2` books 09:15–09:45 vs. the existing 09:00–09:30 | `booked:false`, same doctor-overlap reason |
| `test-book-appointment-back-to-back` | `P-2` books 09:30, starting exactly when the first appointment ends | `booked:true` — touching ranges don't overlap |
| `test-book-appointment-different-doctor` | Same slot (2026-08-10 09:00), two different doctors (`D-100`, `D-200`) | Both `booked:true` — proves the doctor-overlap constraint is per-doctor, not global |
| `test-book-appointment-patient-overlap` | `P-1` (just booked `D-100` above) books `D-200` at an overlapping time | `{"booked":false,"reason":"This patient already has another appointment at that time."}` — exercises `excl_patient_overlap` |
| `test-book-appointment-unknown-doctor` | `doctorId` that doesn't exist | `{"booked":false,"reason":"No doctor found with that ID."}` |
| `test-book-appointment-unknown-patient` | `patientId` that doesn't exist | `{"booked":false,"reason":"No patient found with that ID."}` |
| `test-book-appointment-outside-hours` | `D-200` (08:00–14:00 only) requested at 22:00 | `{"booked":false,"reason":"Requested time falls outside the doctor's working hours."}` |
| `test-book-appointment-invalid-date` | `date:"not-a-date"` | `{"booked":false,"reason":"Invalid date -- expected YYYY-MM-DD."}` |

## Watching logs while testing

Each business function logs its own attempt + outcome (see
[app/main.py](app/main.py)), so you can watch requests land in real time
while running the tests above:

```bash
docker compose logs -f booking_service
```

## Troubleshooting

- **A `/tools/*` endpoint 404s but `/health` returns 200** → the container is
  running stale code. Rebuild: `docker compose up -d --build`.
- **`health-ready` returns 503 / `{"database":"unavailable"}`** → Postgres is
  down or unreachable. Check `docker compose ps` and `docker compose logs db`.
- **`docker inspect <container> --format '{{.State.Health.Status}}'`** shows
  Docker's own periodic healthcheck result (`healthy` / `unhealthy`),
  independent of manually running `make health` yourself.
