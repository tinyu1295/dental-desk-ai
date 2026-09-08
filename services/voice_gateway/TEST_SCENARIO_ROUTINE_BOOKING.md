# Test Scenario — Routine Booking (Sarah Johnson)

A clean, natural end-to-end call scenario — no deliberate stress-testing,
just a realistic booking exercising the whole flow at once. Good as a
"does everything still work together" regression check after any change
to `services/voice_gateway/app/bedrock_stream.py` or `main.py`. This one
is deliberately the "happy path," spoken the way a real first-time caller
actually would.

---

## Setup

```bash
docker compose logs -f voice_gateway
```

## Scenario: Sarah Johnson, first-time patient, routine sensitivity check

**1. Opening**

> _"Hello."_
> _"This is my first time."_

**2. Doctor preference — asked before name/DOB, not after**

> _"Anyone available is fine."_

**3. Name — given whole, naturally**

> _"My name is Sarah Johnson."_

Expect: `collectPatientNameTool(firstName='sarah', lastName='johnson')` →
`complete` in one call. Readback → confirm with **"yes."**

**4. Date of birth**

> _"March third, nineteen ninety-five."_

Expect: `collectDobTool(month=3, day=3, year=1995)`. Readback → **"yes."**

**5. Chief complaint — realistic, non-emergency, asked next, not phone**

> _"
> No swelling, no real pain, just want it checked out."_

Expect `appointmentType` to land on something like `'CheckUp'` or
`'FollowUp'`, not `'Emergency'` — worth checking it doesn't over-triage a
mild complaint. Also worth checking the AI doesn't jump straight from the
confirmed date of birth into asking for a phone number here.

**6. Slot** — accept whatever's offered:

> _"That works, thanks."_

**7. Phone** — say no to the caller-ID offer, then give your number
clearly in two natural groups:

> _"No, let me give you a different number."_
> _"Three one two, eight four seven."_
> _"Five two nine six."_

Expect two `collectPhoneDigitsTool` calls (`'312847'` then `'5296'`),
landing on `'3128475296'` in one pass — no retries, no reset needed.
Readback grouped → **"yes, that's correct."**

**8. Let it register and book.**

**9. Close it out naturally**

> _"No, that's everything, thank you."_

Expect `endCallTool`, a brief farewell, then the call ending on its own —
no manual hangup needed.

---

## Pass/fail criteria

- [ ] Questions come in the right order: doctor preference before name/DOB,
      chief complaint and slot-finding before phone — never phone right
      after date of birth is confirmed.
- [ ] `registerPatientTool` args exactly:
      `firstName: 'sarah', lastName: 'johnson', dob: '1995-03-03', phone: '3128475296'`
      — every field matching what was actually said, digit/letter for
      digit/letter.
- [ ] `bookAppointmentTool` succeeds with a sensible `appointmentType` for
      a mild, non-emergency complaint.
- [ ] No mismatch between any `TOOL CALL (raw)` and the corresponding
      `TOOL CALL (sent)` on the confirmed fields, phone in particular.
- [ ] Clean farewell and hangup — `endCallTool` fires, the caller hears
      the goodbye, the call ends on its own with no manual hangup needed.
- [ ] Database check (optional, but the most reliable confirmation):
  ```bash
  docker compose exec -T db psql -U dental -d dental \
    -c "SELECT patient_id, first_name, last_name, phone, dob FROM patients ORDER BY created_at DESC LIMIT 1;"
  ```

---

## Background: how "available" is decided

Step 6 above ("let it accept whatever's offered") is doing more than it
looks like — the offered slot comes from three separate checks in
`booking_service`, all enforced at the Postgres level, not just app logic:

1. **Working hours** (`doctor_working_hours`) — no row for that
   day-of-week means the doctor doesn't work that day at all.
2. **Time off** (`doctor_time_off`) — a date-range block that shuts out
   an entire day for that doctor regardless of normal hours.
3. **Already-booked time** (`appointments`) — any appointment on that
   doctor's calendar that day counts as occupied _unless_ its status is
   `Cancelled` or `Rescheduled`, in which case the time frees back up.

`findNextOpeningTool` walks the working window in fixed steps (30 min by
default) and returns the first slot that clears all three checks.

### Real example, pulled live from the running stack

**Doctor:** Dr. Marcus Lee (`D-200`) — Monday, 2026-09-07

Working hours:

```
MON  08:00:00 – 14:00:00
```

No time-off entry blocks this date.

Existing appointment already on the books:

```
appt_id     | date       | start_time | end_time | status
A-93a23a2c  | 2026-09-07 | 08:00:00   | 08:30:00 | Booked
```

So **08:00–08:30 is unavailable** — it's `Booked`, not `Cancelled`/
`Rescheduled`. Calling `findNextOpeningTool` for this exact doctor/date
confirms the AI skips it and starts one slot later:

```json
{
  "found": true,
  "date": "2026-09-07",
  "startTime": "08:30",
  "endTime": "09:00",
  "doctorId": "D-200",
  "doctorName": "Dr. Marcus Lee"
}
```

Even though Dr. Lee's day starts at 08:00, the first slot actually
offered is 08:30 — because the earlier half hour is already taken.

## Results

**Status:** ⏳ Not yet run.
