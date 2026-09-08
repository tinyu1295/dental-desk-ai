import logging
import os
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv, find_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import psycopg2
from psycopg2 import errors
from psycopg2.extras import RealDictCursor

load_dotenv(find_dotenv())

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("booking_service")

app = FastAPI()

DATABASE_URL = os.environ.get("DATABASE_URL") or (
    "postgresql://{user}:{password}@{host}:{port}/{db}".format(
        user=os.environ.get("POSTGRES_USER", "dental"),
        password=os.environ.get("POSTGRES_PASSWORD", "dental"),
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        db=os.environ.get("POSTGRES_DB", "dental"),
    )
)
# Must match the CHECK constraint and the appointments.EXCLUDE ... WHERE
# predicate in db.py -- these three have to agree on what "occupied" means.
OCCUPIED_EXCLUDED_STATUSES = ("Cancelled", "Rescheduled")
# Index matches datetime.weekday() (0=Monday) -- must match the day_of_week
# CHECK constraint on doctor_working_hours in db.py.
DAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


@app.get("/health")
def health():
    """Liveness check: is the process up and able to respond? No DB dependency
    on purpose -- a DB hiccup shouldn't make an orchestrator restart this app."""
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    """Readiness check: can this instance actually serve traffic right now?
    Used by load balancers / docker healthchecks to decide whether to route
    to this instance -- a failure here should pull it from rotation, not
    trigger a restart."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        logger.exception("health/ready: database check failed")
        return JSONResponse(status_code=503, content={"status": "error", "database": "unavailable"})
    return {"status": "ok", "database": "connected"}


# ---------- business functions ----------
def _find_patient(first_name: str, last_name: str, dob: str):
    logger.info("find-patient: looking up name=%s %s dob=%s",
                first_name, last_name, dob)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT patient_id, first_name, last_name, phone FROM patients "
            "WHERE lower(first_name) = lower(%s) AND lower(last_name) = lower(%s) AND dob = %s",
            (first_name, last_name, dob),
        )
        row = cur.fetchone()
        if not row:
            logger.info("find-patient: no match for name=%s %s dob=%s",
                        first_name, last_name, dob)
            return {"found": False}

        # "Has this patient been booked/treated before" is a question
        # about appointments, not a new medical-history table -- the data
        # to answer it already exists. Cancelled/Rescheduled don't count
        # as a real visit; anything else (Booked, Completed, NoShow) does.
        cur.execute(
            "SELECT doctor_id, COUNT(*) AS visit_count, MAX(date) AS last_visit "
            "FROM appointments WHERE patient_id = %s AND status NOT IN %s "
            "GROUP BY doctor_id ORDER BY visit_count DESC, last_visit DESC LIMIT 1",
            (row["patient_id"], OCCUPIED_EXCLUDED_STATUSES),
        )
        history = cur.fetchone()

    logger.info(
        "find-patient: matched patientId=%s returningPatient=%s usualDoctorId=%s",
        row["patient_id"],
        history is not None,
        history["doctor_id"] if history else None,
    )
    return {
        "found": True,
        "patientId": row["patient_id"],
        "firstName": row["first_name"],
        "lastName": row["last_name"],
        "phone": row["phone"],
        "returningPatient": history is not None,
        "usualDoctorId": history["doctor_id"] if history else None,
        "lastVisitDate": history["last_visit"].isoformat() if history else None,
    }


def _register_patient(first_name: str, last_name: str, dob: str, phone: str):
    patient_id = f"P-{uuid.uuid4().hex[:8]}"
    logger.info("register-patient: attempting patientId=%s name=%s %s dob=%s",
                patient_id, first_name, last_name, dob)
    with get_conn() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO patients (patient_id, first_name, last_name, dob, phone) "
                "VALUES (%s, %s, %s, %s, %s)",
                (patient_id, first_name, last_name, dob, phone),
            )
            conn.commit()
        except errors.UniqueViolation:
            conn.rollback()
            logger.info("register-patient: duplicate name=%s %s dob=%s",
                        first_name, last_name, dob)
            return {"registered": False, "reason": "A patient with this name and date of birth already exists."}
    logger.info("register-patient: registered patientId=%s", patient_id)
    return {"registered": True, "patientId": patient_id}


def _update_patient_phone(patient_id: str, phone: str):
    logger.info("update-patient-phone: attempting patientId=%s", patient_id)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE patients SET phone = %s WHERE patient_id = %s RETURNING patient_id",
            (phone, patient_id),
        )
        row = cur.fetchone()
        conn.commit()
    if not row:
        logger.info(
            "update-patient-phone: no patient found patientId=%s", patient_id)
        return {"updated": False, "reason": "No patient found with that ID."}
    logger.info("update-patient-phone: updated patientId=%s", patient_id)
    return {"updated": True, "patientId": patient_id, "phone": phone}


def _parse_date(date_str: str):
    """datetime for a YYYY-MM-DD string, or None if it doesn't parse --
    callers that take a date from the outside world (ultimately an LLM
    turning speech into a field) shouldn't 500 on a malformed one."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _parse_time(time_str: str):
    """datetime for an HH:MM string, or None if it doesn't parse. Same
    reasoning as _parse_date."""
    try:
        return datetime.strptime(time_str, "%H:%M")
    except (ValueError, TypeError):
        return None


def _fetch_doctor(cur, doctor_id: str):
    """Row (doctor_id, name) or None. Shared by book-appointment's existence
    check and the standalone doctor-lookup tools so both agree on what a
    doctor record looks like."""
    cur.execute(
        "SELECT doctor_id, name FROM doctors WHERE doctor_id = %s", (doctor_id,))
    return cur.fetchone()


def _get_doctor(doctor_id: str):
    logger.info("get-doctor: looking up doctorId=%s", doctor_id)
    with get_conn() as conn, conn.cursor() as cur:
        row = _fetch_doctor(cur, doctor_id)
    if not row:
        logger.info("get-doctor: no match for doctorId=%s", doctor_id)
        return {"found": False}
    logger.info("get-doctor: matched doctorId=%s name=%s",
                row["doctor_id"], row["name"])
    return {"found": True, "doctorId": row["doctor_id"], "name": row["name"]}


def _compute_slots(day: datetime, hours, booked, step: timedelta):
    """Pure slot math: walks a working window in `step`-sized increments,
    skipping anything that overlaps `booked`. Used by find-next-opening
    so the overlap rule lives in one place, separate from the DB reads
    around it."""
    cursor = datetime.combine(day, hours["start_time"])
    end = datetime.combine(day, hours["end_time"])
    slots = []
    while cursor + step <= end:
        slot_start, slot_end = cursor.time(), (cursor + step).time()
        overlaps = any(slot_start < b_end and slot_end >
                       b_start for b_start, b_end in booked)
        if not overlaps:
            slots.append({"startTime": slot_start.strftime(
                "%H:%M"), "endTime": slot_end.strftime("%H:%M")})
        cursor += step
    return slots


def _get_working_window(cur, doctor_id: str, date_str: str):
    """Returns (hours_row, None) if the doctor works this day and has no
    time off covering it, or (None, reason) otherwise. Shared by
    find-next-opening and book-appointment so both agree on what counts
    as a valid booking window."""
    day_of_week = DAY_CODES[datetime.strptime(date_str, "%Y-%m-%d").weekday()]

    cur.execute(
        "SELECT start_time, end_time FROM doctor_working_hours "
        "WHERE doctor_id = %s AND day_of_week = %s",
        (doctor_id, day_of_week),
    )
    hours = cur.fetchone()
    if not hours:
        return None, "Doctor does not work this day"

    cur.execute(
        "SELECT 1 FROM doctor_time_off "
        "WHERE doctor_id = %s AND %s BETWEEN start_date AND end_date",
        (doctor_id, date_str),
    )
    if cur.fetchone():
        return None, "Doctor is off this day"

    return hours, None


def _find_next_opening(date_from: str, date_to: str, duration_minutes: int = 30, doctor_id: str = None):
    """Earliest open slot in [date_from, date_to] -- for one doctor if
    doctor_id is given, or across every doctor if it isn't. One DB round
    trip covering the whole search window instead of the caller looping
    a per-day, per-doctor check. Doesn't search past date_to on its own --
    extending the window (e.g. into next week) is left to the caller."""
    logger.info("find-next-opening: searching dateFrom=%s dateTo=%s durationMinutes=%s doctorId=%s",
                date_from, date_to, duration_minutes, doctor_id)
    start_day = _parse_date(date_from)
    end_day = _parse_date(date_to)
    if start_day is None or end_day is None:
        logger.info("find-next-opening: invalid date dateFrom=%s dateTo=%s",
                    date_from, date_to)
        return {"found": False, "reason": "Invalid date -- expected YYYY-MM-DD."}

    with get_conn() as conn, conn.cursor() as cur:
        if doctor_id:
            doctor = _fetch_doctor(cur, doctor_id)
            if not doctor:
                logger.info(
                    "find-next-opening: no doctor found doctorId=%s", doctor_id)
                return {"found": False, "reason": "No doctor found with that ID."}
            candidates = [doctor]
        else:
            cur.execute(
                "SELECT doctor_id, name FROM doctors ORDER BY doctor_id")
            candidates = cur.fetchall()
            if not candidates:
                logger.info("find-next-opening: no doctors on file")
                return {"found": False, "reason": "No doctors on file."}

        step = timedelta(minutes=duration_minutes)
        day = start_day

        while day <= end_day:
            date_str = day.strftime("%Y-%m-%d")
            # (slot, doctor) -- earliest slot found today across candidates
            best = None

            for doc in candidates:
                hours, reason = _get_working_window(
                    cur, doc["doctor_id"], date_str)
                if reason:
                    continue  # doesn't work this day, or has time off

                cur.execute(
                    "SELECT start_time, end_time FROM appointments "
                    "WHERE doctor_id = %s AND date = %s AND status NOT IN %s",
                    (doc["doctor_id"], date_str, OCCUPIED_EXCLUDED_STATUSES),
                )
                booked = [(r["start_time"], r["end_time"])
                          for r in cur.fetchall()]
                slots = _compute_slots(day, hours, booked, step)
                if slots and (best is None or slots[0]["startTime"] < best[0]["startTime"]):
                    best = (slots[0], doc)

            if best:
                slot, doc = best
                logger.info(
                    "find-next-opening: found date=%s startTime=%s endTime=%s doctorId=%s",
                    date_str, slot["startTime"], slot["endTime"], doc["doctor_id"])
                return {"found": True, "date": date_str, "startTime": slot["startTime"],
                        "endTime": slot["endTime"], "doctorId": doc["doctor_id"], "doctorName": doc["name"]}

            day += timedelta(days=1)

    logger.info("find-next-opening: no opening found dateFrom=%s dateTo=%s",
                date_from, date_to)
    return {"found": False, "reason": f"No opening between {date_from} and {date_to}."}


def _patient_exists(cur, patient_id: str) -> bool:
    cur.execute("SELECT 1 FROM patients WHERE patient_id = %s", (patient_id,))
    return cur.fetchone() is not None


def _exclusion_violation_reason(e) -> str:
    """appointments has two EXCLUDE constraints -- excl_doctor_overlap
    (a doctor can't be double-booked) and excl_patient_overlap (a patient
    can't be double-booked across two different doctors). Both raise the
    same ExclusionViolation; diag.constraint_name is the only way to
    tell which one fired, so book-appointment and reschedule-appointment
    (Brick 7) share this instead of each guessing from context."""
    if getattr(e.diag, "constraint_name", None) == "excl_patient_overlap":
        return "This patient already has another appointment at that time."
    return "That time overlaps an existing appointment for this doctor. Please choose another."


def _book_appointment(patient_id: str, doctor_id: str, date_str: str, start_time: str,
                       duration_minutes: int, appointment_type: str, reason: str):
    logger.info(
        "book-appointment: attempting patientId=%s doctorId=%s date=%s startTime=%s durationMinutes=%s",
        patient_id, doctor_id, date_str, start_time, duration_minutes)
    if _parse_date(date_str) is None:
        logger.info("book-appointment: invalid date date=%s", date_str)
        return {"booked": False, "reason": "Invalid date -- expected YYYY-MM-DD."}
    start_dt = _parse_time(start_time)
    if start_dt is None:
        logger.info("book-appointment: invalid start time startTime=%s", start_time)
        return {"booked": False, "reason": "Invalid start time -- expected HH:MM."}
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    end_time = end_dt.strftime("%H:%M")
    appt_id = f"A-{uuid.uuid4().hex[:8]}"

    with get_conn() as conn, conn.cursor() as cur:
        doctor = _fetch_doctor(cur, doctor_id)
        if not doctor:
            logger.info("book-appointment: no doctor found doctorId=%s", doctor_id)
            return {"booked": False, "reason": "No doctor found with that ID."}
        if not _patient_exists(cur, patient_id):
            logger.info("book-appointment: no patient found patientId=%s", patient_id)
            return {"booked": False, "reason": "No patient found with that ID."}

        hours, block_reason = _get_working_window(cur, doctor_id, date_str)
        if block_reason:
            logger.info("book-appointment: rejected doctorId=%s date=%s reason=%s",
                        doctor_id, date_str, block_reason)
            return {"booked": False, "reason": block_reason}
        if not (hours["start_time"] <= start_dt.time() and end_dt.time() <= hours["end_time"]):
            logger.info(
                "book-appointment: outside working hours doctorId=%s date=%s startTime=%s endTime=%s",
                doctor_id, date_str, start_time, end_time)
            return {"booked": False, "reason": "Requested time falls outside the doctor's working hours."}

        try:
            cur.execute(
                "INSERT INTO appointments "
                "(appt_id, patient_id, doctor_id, date, start_time, end_time, "
                " duration_minutes, status, appointment_type, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'Booked', %s, %s)",
                (appt_id, patient_id, doctor_id, date_str, start_time, end_time,
                 duration_minutes, appointment_type, reason),
            )
            conn.commit()
        except errors.ExclusionViolation as e:
            conn.rollback()
            violation_reason = _exclusion_violation_reason(e)
            logger.info("book-appointment: exclusion violation patientId=%s doctorId=%s reason=%s",
                        patient_id, doctor_id, violation_reason)
            return {"booked": False, "reason": violation_reason}
        except errors.ForeignKeyViolation:
            conn.rollback()
            logger.info("book-appointment: foreign key violation patientId=%s doctorId=%s",
                        patient_id, doctor_id)
            return {"booked": False, "reason": "Unknown patient or doctor ID."}

    logger.info("book-appointment: booked apptId=%s patientId=%s doctorId=%s date=%s startTime=%s",
                appt_id, patient_id, doctor_id, date_str, start_time)
    return {"booked": True, "apptId": appt_id, "doctorId": doctor_id, "doctorName": doctor["name"],
            "date": date_str, "startTime": start_time, "endTime": end_time}


# ---------- request models ----------
class FindPatientRequest(BaseModel):
    firstName: str
    lastName: str
    dob: str


class RegisterPatientRequest(BaseModel):
    firstName: str
    lastName: str
    dob: str
    phone: str


class UpdatePatientPhoneRequest(BaseModel):
    patientId: str
    phone: str


class GetDoctorRequest(BaseModel):
    doctorId: str


class FindNextOpeningRequest(BaseModel):
    dateFrom: str
    dateTo: str
    durationMinutes: int = Field(default=30, gt=0)
    doctorId: str | None = None


class BookAppointmentRequest(BaseModel):
    patientId: str
    doctorId: str
    date: str
    startTime: str
    durationMinutes: int = Field(default=30, gt=0)
    appointmentType: str
    reason: str


# ---------- routes ----------
@app.post("/tools/find-patient")
def find_patient(req: FindPatientRequest):
    return _find_patient(req.firstName, req.lastName, req.dob)


@app.post("/tools/register-patient")
def register_patient(req: RegisterPatientRequest):
    return _register_patient(req.firstName, req.lastName, req.dob, req.phone)


@app.post("/tools/update-patient-phone")
def update_patient_phone(req: UpdatePatientPhoneRequest):
    return _update_patient_phone(req.patientId, req.phone)


@app.post("/tools/get-doctor")
def get_doctor(req: GetDoctorRequest):
    return _get_doctor(req.doctorId)


@app.post("/tools/find-next-opening")
def find_next_opening(req: FindNextOpeningRequest):
    return _find_next_opening(req.dateFrom, req.dateTo, req.durationMinutes, req.doctorId)


@app.post("/tools/book-appointment")
def book_appointment(req: BookAppointmentRequest):
    return _book_appointment(
        req.patientId, req.doctorId, req.date, req.startTime,
        req.durationMinutes, req.appointmentType, req.reason,
    )
