"""
Employee Attendance & Analytics API

FastAPI application providing employee management, attendance lifecycle (punch-in, punch-out,
regularization), and advanced analytics via MongoDB aggregation pipelines.
"""

import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

import bson.json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration & Constants
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)

STATUS_PRESENT = "PRESENT"
STATUS_WFH = "WFH"
STATUS_ON_DUTY = "ON_DUTY"
STATUS_ABSENT = "ABSENT"
STATUS_LEAVE = "LEAVE"

ALL_STATUSES = (STATUS_PRESENT, STATUS_WFH, STATUS_ON_DUTY, STATUS_ABSENT, STATUS_LEAVE)
PRESENCE_STATUSES = (STATUS_PRESENT, STATUS_WFH, STATUS_ON_DUTY)

GRACE_SECONDS = 600
OVERTIME_MIN_MINUTES = 30
HALF_DAY_THRESHOLD = Decimal("4.50")
MAX_PUNCH_SPAN_SECONDS = 86400
MAX_TREND_DAYS = 92

MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000

DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIME_REGEX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMP_CODE_REGEX = re.compile(r"^EMP\d{4,6}$")
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

logger = logging.getLogger("uvicorn.error")

# --------------------------------------------------------------------------- #
# 2. Database Client
# --------------------------------------------------------------------------- #
client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=2000,
)
db = client[MONGO_DB]


# --------------------------------------------------------------------------- #
# 3. Pure Helpers
# --------------------------------------------------------------------------- #
def unprocessable(loc: tuple, msg: str) -> RequestValidationError:
    """Construct a FastAPI-standard RequestValidationError matching openapi detail array shape."""
    return RequestValidationError([{"loc": list(loc), "msg": msg, "type": "value_error"}])


def ms_to_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime truncated to whole seconds (R1)."""
    s = (ms - ms % 1000) // 1000
    return EPOCH_UTC + timedelta(seconds=s)


def dt_to_ms(dt: datetime) -> int:
    """Convert a datetime to exact integer epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt - EPOCH_UTC) // timedelta(milliseconds=1))


def now_dt() -> datetime:
    """Current UTC datetime truncated to whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hhmm(s: str) -> tuple[int, int]:
    """Parse HH:MM string to (hour, minute)."""
    h, m = map(int, s.split(":"))
    return h, m


def is_overnight(start: str, end: str) -> bool:
    """Check if shift spans overnight: shift_end <= shift_start (R1)."""
    return end <= start


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Compute IST start and end datetimes for a shift given its attendance date (R1, R2, R3)."""
    d = date.fromisoformat(date_str)
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    start_dt = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end_d = d + timedelta(days=1)
    else:
        end_d = d
    end_dt = datetime(end_d.year, end_d.month, end_d.day, eh, em, 0, tzinfo=IST)
    return start_dt, end_dt


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Compute attendance date YYYY-MM-DD per Rule R1.
    For overnight shifts, a punch-in strictly earlier than shift_end belongs to the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    cal_date = punch_in_ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        punch_tod = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
        shift_end_tod = (eh, em, 0)
        if punch_tod < shift_end_tod:
            cal_date = cal_date - timedelta(days=1)
    return cal_date.isoformat()


def late_minutes_for(punch_in_utc: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """Compute late minutes per Rule R2: grace period 600s; floor(seconds / 60) since shift_start."""
    start_dt, _ = shift_bounds(date_str, shift_start, shift_end)
    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_sec = int((punch_in_ist - start_dt).total_seconds())
    if diff_sec > GRACE_SECONDS:
        return diff_sec // 60
    return 0


def overtime_for(punch_out_utc: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """Compute overtime minutes per Rule R3: >= 30 minutes after shift_end, else 0."""
    _, end_dt = shift_bounds(date_str, shift_start, shift_end)
    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_sec = int((punch_out_ist - end_dt).total_seconds())
    mins = diff_sec // 60
    if mins >= OVERTIME_MIN_MINUTES:
        return mins
    return 0


def work_hours_for(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Compute work hours per Rule R4: (out - in) seconds / 3600, rounded half-up to 2 dp."""
    diff_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    val = (Decimal(diff_sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(val)


def half_day_for(work_hours: Optional[float]) -> bool:
    """Compute half-day status per Rule R5: rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return Decimal(str(work_hours)) < HALF_DAY_THRESHOLD


def r8(x: Any, places: int) -> Optional[float]:
    """Format reported floating point values to given decimals using Decimal ROUND_HALF_UP (R8)."""
    if x is None:
        return None
    pattern = Decimal("10") ** -places
    return float(Decimal(str(x)).quantize(pattern, rounding=ROUND_HALF_UP))


def derive(
    status_val: str,
    punch_in_utc: Optional[datetime],
    punch_out_utc: Optional[datetime],
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> dict:
    """
    Unified authoritative calculation of derived fields per DATA_MODEL status table:
    (work_hours, late_minutes, overtime_minutes, half_day).
    """
    if status_val in PRESENCE_STATUSES:
        if punch_out_utc is None:
            work_hours = None
            late_minutes = late_minutes_for(punch_in_utc, date_str, shift_start, shift_end) if punch_in_utc else 0
            overtime_minutes = 0
            half_day = False
        else:
            work_hours = work_hours_for(punch_in_utc, punch_out_utc)
            late_minutes = late_minutes_for(punch_in_utc, date_str, shift_start, shift_end) if punch_in_utc else 0
            overtime_minutes = overtime_for(punch_out_utc, date_str, shift_start, shift_end)
            half_day = half_day_for(work_hours)
    else:
        work_hours = None
        late_minutes = 0
        overtime_minutes = 0
        half_day = False
    return {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def parse_date_strict(val: str, loc: tuple) -> date:
    """Strict calendar date parsing with regex check and year >= 1."""
    if not isinstance(val, str) or not DATE_REGEX.match(val):
        raise unprocessable(loc, "Invalid date format, expected YYYY-MM-DD")
    try:
        d = date.fromisoformat(val)
        if d.year < 1:
            raise ValueError
        return d
    except ValueError:
        raise unprocessable(loc, "Invalid calendar date")


def parse_month_strict(val: str, loc: tuple) -> tuple[int, int]:
    """Strict calendar month parsing with regex check and year >= 1."""
    if not isinstance(val, str) or not MONTH_REGEX.match(val):
        raise unprocessable(loc, "Invalid month format, expected YYYY-MM")
    y, m = map(int, val.split("-"))
    if y < 1:
        raise unprocessable(loc, "Invalid year")
    return y, m


# --------------------------------------------------------------------------- #
# 4. Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_rules(self):
        if not DATE_REGEX.match(self.joined_on):
            raise ValueError("joined_on must match YYYY-MM-DD")
        try:
            d = date.fromisoformat(self.joined_on)
            if d.year < 1:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on is not a valid calendar date")
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def reject_explicit_null(cls, v, info):
        if v is None:
            raise ValueError(f"{info.field_name} cannot be null")
        return v


# --------------------------------------------------------------------------- #
# 5. Serialization
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    """Format employee document strictly to schema: no _id, created_at as epoch ms."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_history_entry(entry: dict) -> dict:
    """Format history entry: convert at and nested punch times from BSON datetime to epoch ms."""
    changes_out = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = dt_to_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_ms(to_val)
        changes_out[k] = {"from": from_val, "to": to_val}
    return {
        "at": dt_to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes_out,
    }


def serialize_record(doc: dict) -> dict:
    """Format attendance record: epoch ms instants, legacy defaults, no internal _id."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": dt_to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }




# --------------------------------------------------------------------------- #
# 7. Lifespan & Index Creation
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create indexes at application startup idempotently without failing startup on errors."""
    try:
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("emp_code", 1)])
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1)])

        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        logger.info("MongoDB indexes verified / created successfully.")
    except Exception as e:
        logger.error(f"Non-fatal error creating indexes: {e}")
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

