# Booking Service — Tool Reference (for the calling LLM)

This document describes `booking_service` as a set of tools for an LLM that is
running a phone conversation (see the call-flow transcript this doc is built
from) and needs to turn what the caller says into HTTP calls. It covers every
route, the request/response shape of each, the database tables and
constraints behind them, and how the tools chain together to produce the
booking flow.

Source: [app/main.py](app/main.py) (routes + business logic),
[app/db.py](app/db.py) (schema + seed data). Manual test scenarios and exact
expected payloads live in [TESTING.md](TESTING.md) — this doc is the "what to
call and when," that one is the "proof it works."

Base URL: `http://localhost:8000` (or `$HOST` — see docker-compose.yml).
All tools are `POST /tools/<name>` with a JSON body, no auth. Every tool
returns HTTP `200` even for "not found" / "rejected" outcomes — failure is
signalled *in the JSON body* (`found: false`, `booked: false`, etc.), not by
HTTP status. The only non-200s you'll see are `422` from Pydantic when a
field is the wrong type or fails a constraint (e.g. `durationMinutes: 0`),
and `503` from `/health/ready` when Postgres is unreachable. Treat a `422`
as a bug in the arguments you sent, not as a normal "no" answer from the
tool.

---

## 1. Data model

Five tables, created fresh by `python db.py` (`make seed-db`). All primary
keys are app-generated strings, not serials — `patients.patient_id` looks
like `P-xxxxxxxx`, `appointments.appt_id` like `A-xxxxxxxx` (8 hex chars,
from `uuid4`), doctors are seeded directly as `D-100` / `D-200`.

| Table | Columns | Notes |
|---|---|---|
| `patients` | `patient_id` PK, `first_name`, `last_name`, `dob` (DATE), `phone`, `created_at` | Identity is `(lower(last_name), lower(first_name), dob)` — a unique index enforces this, and `find-patient`/`register-patient` both key off it. Two patients can share a name as long as DOB differs. |
| `doctors` | `doctor_id` PK, `name` | Seeded: `D-100` = Dr. Elena Cruz, `D-200` = Dr. Marcus Lee. No self-service creation — doctors are fixed data. |
| `doctor_working_hours` | `doctor_id`, `day_of_week` (`MON`…`SUN`), `start_time`, `end_time` | PK `(doctor_id, day_of_week)` — one row per day the doctor works. A day with no row means the doctor doesn't work that day at all. |
| `doctor_time_off` | `time_off_id` PK (serial), `doctor_id`, `start_date`, `end_date`, `reason` | Inclusive date range. Overrides `doctor_working_hours` for the covered dates (e.g. seeded: Dr. Cruz off 2026-08-15 for training). |
| `appointments` | `appt_id` PK, `patient_id` FK, `doctor_id` FK, `date`, `start_time`, `end_time`, `duration_minutes`, `status`, `appointment_type`, `reason`, `created_at`, `time_range` (generated) | See below — this table is where all the collision logic lives. |

**`appointments.status`** is one of `Booked`, `Completed`, `Cancelled`,
`Rescheduled`, `NoShow` (CHECK constraint). `Cancelled` and `Rescheduled`
are the two statuses that **don't** hold a slot — they're excluded from
overlap checks and from "has this patient been treated before" history.
This set (`OCCUPIED_EXCLUDED_STATUSES` in main.py) has to agree across three
places: the CHECK constraint, the two EXCLUDE constraints below, and
`_find_patient`'s history query — if you ever see it change, all three
need to move together.

**Double-booking prevention** is enforced by Postgres itself, not app
logic — two `EXCLUDE USING gist` constraints on `appointments`:

- `excl_doctor_overlap` — same `doctor_id`, overlapping `time_range`, both
  in an occupying status → rejected. Stops one doctor being double-booked.
- `excl_patient_overlap` — same `patient_id`, overlapping `time_range` →
  rejected. Stops one patient being booked with two different doctors at
  once.

`time_range` is a generated `tsrange(date + start_time, date + end_time,
'[)')` column — half-open, so a 09:00–09:30 appointment and a back-to-back
09:30–10:00 appointment do **not** overlap. Both constraints raise the same
`ExclusionViolation`; `book-appointment` tells them apart via
`e.diag.constraint_name` and returns a different `reason` string for each
(see the `book-appointment` section).

There is no `DELETE` anywhere in the app — appointments are only ever
inserted or (in a future Brick 7, per the code comments) transitioned via
status/reschedule, never removed.

---

## 2. Flow → tool map

This is the call flow you described, condensed to just the decision points
and which tool fires at each:

```
caller says "book an appointment"
        │
        ▼
"first time here?" ──── no (returning) ────► find-patient(first, last, dob)
        │                                            │
       yes                                found=true?├─ usualDoctorId present? ─yes─► get-doctor(doctorId)
        │                                            │         │
        │                                            │         no → doctorId stays unset ("anyone is fine" path)
        │                                            no → treat as first-time (see note below)
        │                                                       │
        ▼                                                       │
  "preferred dentist?" → doctorId set, or left unset             │
  ("anyone is fine")                                              │
        │                                                       │
        └───────────────────────────┬───────────────────────────┘
                                     ▼
                    gather chief complaint (no tool call — carried
                    forward as `reason` on book-appointment)
                                     │
                                     ▼
                    find-next-opening(doctorId, dateFrom=today,
                                       dateTo=end of this week)
                                     │
                  found? ──yes──► report slot to caller
                     │
                     no → offer: wait until next week (re-call with
                          dateFrom/dateTo shifted, same doctorId) OR
                          see someone sooner (re-call with doctorId=None,
                          same this-week window)
                                     │
                                     ▼
                    caller accepts a slot (no tool call — just picks it)
                                     │
              returning patient?  ───┴─── first-time patient?
                     │                          │
                     ▼                          ▼
     update-patient-phone(patientId, phone)   register-patient(first, last,
                     │                          dob, phone)  →  patientId
                     │                          │
                     └──────────────┬───────────┘
                                    ▼
                    book-appointment(patientId, doctorId, date,
                      startTime, durationMinutes, appointmentType, reason)
                                    │
                     booked:true ───┴─── booked:false
                          │                    │
                    confirm to caller    report `reason`, loop back to
                                         find-next-opening for another slot
```

Note on `find-patient` returning `found: false` for a caller who claimed to
be "returning": the tool only tells you whether a patient row matches; it
doesn't gate which script branch you're on. If it comes back `false`,
fall back to the first-time script (ask name/DOB/phone and
`register-patient`) rather than dead-ending the call.

---

## 3. Tools

### `find-patient` — `POST /tools/find-patient`

**When:** Returning patient, right after they give name + DOB. Not called
for first-time patients — there's nothing to look up yet.

**Request**
```json
{ "firstName": "Anna", "lastName": "Smith", "dob": "1991-06-05" }
```
`dob` is `YYYY-MM-DD`. Matching is case-insensitive on both name fields
(`lower()` on both sides), `dob` compared exactly.

**Response — match**
```json
{
  "found": true,
  "patientId": "P-1",
  "firstName": "Anna",
  "lastName": "Smith",
  "phone": "+15551234567",
  "returningPatient": true,
  "usualDoctorId": "D-100",
  "lastVisitDate": "2026-08-04"
}
```
`returningPatient` / `usualDoctorId` / `lastVisitDate` come from the
patient's appointment history, not a separate field on the patient row:
the doctor they've seen most often (ties broken by most recent) among
appointments *not* `Cancelled`/`Rescheduled`. If the patient row exists but
has no such appointments, `returningPatient` is `false` and
`usualDoctorId`/`lastVisitDate` are `null` — use this to decide whether to
say "I see you've been treated by Dr X before."

**Response — no match**
```json
{ "found": false }
```

**Next tool if `usualDoctorId` present:** `get-doctor(doctorId)` to get the
name to say back to the caller ("Dr Smith"). If `usualDoctorId` is `null`,
skip straight to `find-next-opening` with `doctorId: null`.

---

### `register-patient` — `POST /tools/register-patient`

**When:** First-time patient, after chief complaint + slot are settled and
you've collected name/DOB/phone. Also the fallback if a self-described
"returning" caller came back `found:false` from `find-patient`.

**Request**
```json
{ "firstName": "John", "lastName": "Doe", "dob": "1998-03-12", "phone": "+15550001111" }
```

**Response — success**
```json
{ "registered": true, "patientId": "P-a1b2c3d4" }
```
Use the returned `patientId` in the following `book-appointment` call.

**Response — duplicate**
```json
{ "registered": false, "reason": "A patient with this name and date of birth already exists." }
```
Fires on the same `(lower(last_name), lower(first_name), dob)` unique
index `find-patient` reads from. If you hit this, the caller is actually
an existing patient under those exact details — recover by calling
`find-patient` with the same name/DOB instead of retrying `register-patient`.

---

### `update-patient-phone` — `POST /tools/update-patient-phone`

**When:** Returning patient, after they confirm the booking slot — collect
a current phone number and write it, in case it's changed since their last
visit.

**Request**
```json
{ "patientId": "P-1", "phone": "+15559990000" }
```

**Response — success**
```json
{ "updated": true, "patientId": "P-1", "phone": "+15559990000" }
```

**Response — unknown patient**
```json
{ "updated": false, "reason": "No patient found with that ID." }
```
Shouldn't happen in the normal flow since `patientId` came from
`find-patient` moments earlier — if it does, something upstream is wrong
(stale ID), not a caller-input problem.

---

### `get-doctor` — `POST /tools/get-doctor`

**When:** Right after `find-patient` returns a `usualDoctorId`, to resolve
it to a human name for the script line "I see you've been treated by
Dr X before."

**Request**
```json
{ "doctorId": "D-100" }
```

**Response — found**
```json
{ "found": true, "doctorId": "D-100", "name": "Dr. Elena Cruz" }
```

**Response — not found**
```json
{ "found": false }
```

---

### `find-next-opening` — `POST /tools/find-next-opening`

**When:** After chief complaint is gathered, for both branches (usual
doctor vs. "anyone is fine"), and again any time the caller rejects a
proposed slot or asks to widen the search (later week, different doctor).

**Request**
```json
{
  "dateFrom": "2026-08-12",
  "dateTo": "2026-08-16",
  "durationMinutes": 30,
  "doctorId": "D-100"
}
```
- `doctorId` omitted or `null` → searches across every doctor and returns
  the single earliest slot among all of them (ties broken toward whichever
  doctor's window opens earlier that day, not by `doctorId` sort order).
- `durationMinutes` defaults to `30`, must be `> 0` (violating this is a
  `422`, not a `found:false`).
- The search never looks past `dateTo` on its own. "This week didn't have
  anything, try next week" is two separate calls with the window shifted
  forward by you, not one call with a wider range implied.

**Response — found**
```json
{
  "found": true,
  "date": "2026-08-14",
  "startTime": "09:00",
  "endTime": "09:30",
  "doctorId": "D-100",
  "doctorName": "Dr. Elena Cruz"
}
```
Always the *earliest* slot in the window — walks day by day, and within a
day picks the doctor whose next opening starts soonest. `doctorName` comes
back even when you passed a specific `doctorId`, so you don't need a
follow-up `get-doctor` call just to say the name to the caller.

**Response — not found**
```json
{ "found": false, "reason": "No opening between 2026-08-12 and 2026-08-12." }
```
Other `reason` values: `"No doctor found with that ID."` (bad `doctorId`),
`"No doctors on file."` (no doctors at all — shouldn't occur outside a
broken seed), `"Invalid date -- expected YYYY-MM-DD."` (malformed
`dateFrom`/`dateTo`).

A day only counts as available if the doctor has a
`doctor_working_hours` row for that weekday **and** no `doctor_time_off`
row covering the date — a day off (weekend, or an explicit time-off
range) is silently skipped, not reported as a separate failure.

---

### `book-appointment` — `POST /tools/book-appointment`

**When:** Last step, once patient identity (`patientId` from
`find-patient`/`register-patient`), `doctorId` + `date` + `startTime`
(from `find-next-opening`, as accepted by the caller), and the chief
complaint (as `reason`) are all in hand.

**Request**
```json
{
  "patientId": "P-1",
  "doctorId": "D-100",
  "date": "2026-08-14",
  "startTime": "09:00",
  "durationMinutes": 30,
  "appointmentType": "CheckUp",
  "reason": "Toothache for two days, worse with hot/cold food and drink"
}
```
`appointmentType` is a free-text label (no CHECK constraint on it in the
schema — seed data uses values like `"CheckUp"`). `reason` is the chief
complaint gathered earlier in the call; there's no separate tool for it,
it's just a field here.

**Response — success**
```json
{
  "booked": true,
  "apptId": "A-9f8e7d6c",
  "doctorId": "D-100",
  "doctorName": "Dr. Elena Cruz",
  "date": "2026-08-14",
  "startTime": "09:00",
  "endTime": "09:30"
}
```
`endTime` is computed server-side (`startTime + durationMinutes`) — don't
compute it yourself for the confirmation line, use what comes back.

**Response — rejected**, all shape `{"booked": false, "reason": "..."}`:

| `reason` | Cause | What to do |
|---|---|---|
| `"No doctor found with that ID."` | `doctorId` doesn't exist | Shouldn't happen if `doctorId` came from a tool response — check for a stale/typo'd ID. |
| `"No patient found with that ID."` | `patientId` doesn't exist | Same — verify it came from `find-patient`/`register-patient` in this call. |
| `"Requested time falls outside the doctor's working hours."` | Slot isn't within that doctor's hours for that weekday (or the doctor doesn't work that day / is off) | Re-run `find-next-opening` — don't re-propose the same slot. |
| `"That time overlaps an existing appointment for this doctor. Please choose another."` | `excl_doctor_overlap` — someone else already holds that doctor's time | Someone booked it between your search and this call (race) — re-run `find-next-opening` for a fresh slot. |
| `"This patient already has another appointment at that time."` | `excl_patient_overlap` — this patient already has a *different* appointment overlapping this time | Ask the caller which appointment they want to keep, or pick a non-overlapping time. |
| `"Invalid date -- expected YYYY-MM-DD."` | Malformed `date` | Only reachable if a date wasn't normalized upstream — `find-next-opening` always returns well-formed dates. |
| `"Invalid start time -- expected HH:MM."` | Malformed `startTime` | Same caveat. |

Every rejection loops back to `find-next-opening` for a new candidate —
there's no "force book" or override path.

---

### `GET /health` and `GET /health/ready`

Not part of the conversation flow — infrastructure checks only.
`/health` confirms the process is up (no DB call, always `200` once the
container starts). `/health/ready` runs `SELECT 1` and returns `503` if
Postgres is unreachable. Not tools the calling LLM needs to invoke.

---

## 4. Two worked examples

**Returning patient, usual doctor has an opening this week:**
1. `find-patient({firstName, lastName, dob})` → `found:true`,
   `usualDoctorId:"D-100"`
2. `get-doctor({doctorId:"D-100"})` → `name:"Dr. Elena Cruz"` — say it back
3. `find-next-opening({doctorId:"D-100", dateFrom:today, dateTo:end-of-week})`
   → `found:true, date, startTime`
4. `update-patient-phone({patientId, phone})`
5. `book-appointment({patientId, doctorId:"D-100", date, startTime, ...})`
   → `booked:true`

**First-time patient, "anyone is fine":**
1. (no `find-patient` — nothing to look up yet)
2. `find-next-opening({doctorId:null, dateFrom:today, dateTo:end-of-week})`
   → `found:true, doctorId:"D-200", doctorName:"Dr. Marcus Lee", date, startTime`
3. `register-patient({firstName, lastName, dob, phone})` → `patientId`
4. `book-appointment({patientId, doctorId:"D-200", date, startTime, ...})`
   → `booked:true`

**Usual doctor has nothing this week, caller wants someone sooner:**
1. `find-patient(...)` → `usualDoctorId:"D-100"`
2. `get-doctor({doctorId:"D-100"})` → name
3. `find-next-opening({doctorId:"D-100", dateFrom:today, dateTo:end-of-week})`
   → `found:false`
4. Caller: "see someone sooner" →
   `find-next-opening({doctorId:null, dateFrom:today, dateTo:end-of-week})`
   → `found:true, doctorId:"D-200"` (a different doctor than usual)
5. `update-patient-phone(...)` → `book-appointment(..., doctorId:"D-200", ...)`
