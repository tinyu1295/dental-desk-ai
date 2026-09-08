import os
import psycopg2
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

DATABASE_URL = os.environ.get("DATABASE_URL") or (
    "postgresql://{user}:{password}@{host}:{port}/{db}".format(
        user=os.environ.get("POSTGRES_USER", "dental"),
        password=os.environ.get("POSTGRES_PASSWORD", "dental"),
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        db=os.environ.get("POSTGRES_DB", "dental"),
    )
)

SCHEMA = """
DROP TABLE IF EXISTS appointments CASCADE;
DROP TABLE IF EXISTS doctor_time_off CASCADE;
DROP TABLE IF EXISTS doctor_working_hours CASCADE;
DROP TABLE IF EXISTS doctors CASCADE;
DROP TABLE IF EXISTS patients CASCADE;

CREATE TABLE patients (
    patient_id      TEXT PRIMARY KEY,
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    dob             DATE NOT NULL,
    phone           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Case-insensitive identity match: "Anna Smith" and "anna smith" must be
-- the same patient. A plain UNIQUE(last_name, first_name, dob) is
-- case-sensitive (raw text comparison) and would let a case-varied
-- duplicate through -- find-patient already normalizes with lower() on
-- both sides, so the write-side constraint needs to match or the two
-- silently disagree.
CREATE UNIQUE INDEX idx_patients_identity ON patients (lower(last_name), lower(first_name), dob);


CREATE TABLE doctors (
    doctor_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL
);

CREATE TABLE doctor_working_hours (
    doctor_id       TEXT NOT NULL REFERENCES doctors(doctor_id),
    day_of_week     TEXT NOT NULL CHECK (day_of_week IN ('MON','TUE','WED','THU','FRI','SAT','SUN')),
    start_time      TIME NOT NULL,
    end_time        TIME NOT NULL,
    PRIMARY KEY (doctor_id, day_of_week)
);

CREATE TABLE doctor_time_off (
    time_off_id     SERIAL PRIMARY KEY,
    doctor_id       TEXT NOT NULL REFERENCES doctors(doctor_id),
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    reason          TEXT
);
CREATE INDEX idx_time_off_doctor_date ON doctor_time_off (doctor_id, start_date, end_date);

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE appointments (
    appt_id             TEXT PRIMARY KEY,
    patient_id          TEXT NOT NULL REFERENCES patients(patient_id),
    doctor_id           TEXT NOT NULL REFERENCES doctors(doctor_id),
    date                DATE NOT NULL,
    start_time          TIME NOT NULL,
    end_time            TIME NOT NULL,
    duration_minutes    INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'Booked'
                             CHECK (status IN ('Booked', 'Completed', 'Cancelled', 'Rescheduled', 'NoShow')),
    appointment_type    TEXT NOT NULL,
    reason              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    time_range          TSRANGE GENERATED ALWAYS AS (
                             tsrange(date + start_time, date + end_time, '[)')
                         ) STORED,
    -- Only "occupying" statuses hold the doctor's calendar -- a cancelled
    -- or rescheduled row's old time_range must stop blocking that slot.
    CONSTRAINT excl_doctor_overlap EXCLUDE USING gist (doctor_id WITH =, time_range WITH &&)
        WHERE (status NOT IN ('Cancelled', 'Rescheduled')),
    -- Same idea, keyed on the patient instead -- stops one patient being
    -- booked with two different doctors at overlapping times. Named
    -- separately from excl_doctor_overlap so the app can tell the two
    -- apart (via the exception's diag.constraint_name) and return the
    -- right reason string for each.
    CONSTRAINT excl_patient_overlap EXCLUDE USING gist (patient_id WITH =, time_range WITH &&)
        WHERE (status NOT IN ('Cancelled', 'Rescheduled'))
);
CREATE INDEX idx_appointments_patient ON appointments (patient_id);
CREATE INDEX idx_appointments_doctor_date ON appointments (doctor_id, date);

"""


def setup_demo_data():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    print("--- Resetting Database ---")
    cur.execute(SCHEMA)
    conn.commit()
    print("Schema created: patients, doctors, doctor_working_hours, doctor_time_off, appointments")

    print("\n--- Seeding Doctors ---")
    cur.execute("INSERT INTO doctors (doctor_id, name) VALUES (%s, %s)",
                ("D-100", "Dr. Elena Cruz"))
    cur.execute("INSERT INTO doctors (doctor_id, name) VALUES (%s, %s)",
                ("D-200", "Dr. Marcus Lee"))
    conn.commit()
    print("Doctors seeded: Dr. Elena Cruz (D-100), Dr. Marcus Lee (D-200)")

    print("\n--- Seeding Doctor Working Hours ---")
    working_hours = [
        ("D-100", "MON", "09:00", "17:00"),
        ("D-100", "TUE", "09:00", "17:00"),
        ("D-100", "WED", "10:00", "18:00"),
        ("D-100", "THU", "09:00", "17:00"),
        ("D-100", "FRI", "09:00", "15:00"),
        ("D-200", "MON", "08:00", "14:00"),
        ("D-200", "WED", "08:00", "14:00"),
        ("D-200", "FRI", "08:00", "14:00"),
    ]
    for doctor_id, day, start, end in working_hours:
        cur.execute(
            "INSERT INTO doctor_working_hours (doctor_id, day_of_week, start_time, end_time) "
            "VALUES (%s, %s, %s, %s)",
            (doctor_id, day, start, end),
        )
    conn.commit()
    print(f"Working hours seeded for {len(working_hours)} doctor-day rows")

    print("\n--- Seeding Doctor Time Off ---")
    cur.execute(
        "INSERT INTO doctor_time_off (doctor_id, start_date, end_date, reason) VALUES (%s, %s, %s, %s)",
        ("D-100", "2026-08-15", "2026-08-15", "Training"),
    )
    conn.commit()
    print("Time off seeded: Dr. Cruz - Training on 2026-08-15")

    print("\n--- Seeding Patients ---")
    cur.execute(
        "INSERT INTO patients (patient_id, first_name, last_name, dob, phone) VALUES (%s, %s, %s, %s, %s)",
        ("P-1", "Anna", "Smith", "1991-06-05", "+15551234567"),
    )
    cur.execute(
        "INSERT INTO patients (patient_id, first_name, last_name, dob, phone) VALUES (%s, %s, %s, %s, %s)",
        ("P-2", "Mark", "Johnson", "1985-01-21", "+15559876543"),
    )
    conn.commit()
    print("Patients seeded: Anna Smith (P-1), Mark Johnson (P-2)")

    print("\n--- Seeding Appointments ---")
    cur.execute(
        "INSERT INTO appointments "
        "(appt_id, patient_id, doctor_id, date, start_time, end_time, "
        " duration_minutes, status, appointment_type, reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        ("A-1", "P-1", "D-100", "2026-08-04", "10:00", "10:30",
         30, "Completed", "CheckUp", "Initial consultation"),
    )
    conn.commit()
    print("Appointments seeded: A-1 - Anna Smith with Dr. Cruz, 2026-08-04, Completed")

    cur.close()
    conn.close()
    print("\n--- Setup Complete ---")


if __name__ == "__main__":
    setup_demo_data()
