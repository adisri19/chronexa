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
# 6. Query and Pipeline Builders (Shared with /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = None,
) -> dict:
    """Build find filter for attendance logs."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter
    return q


def build_employee_monthly_pipeline(emp_code: str, start_date: str, end_date: str) -> list:
    """Build aggregation pipeline on attendance_logs for employee monthly summary."""
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$group": {
                "_id": None,
                "present2": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {
                                        "$in": [
                                            {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [1, 2, 3, 4, 5],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", STATUS_LEAVE]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


def build_department_summary_pipeline(
    start_date: str,
    end_date: str,
    department: Optional[str] = None,
) -> list:
    """Build cross-collection aggregation pipeline rooted on employees for department summary."""
    match_emp: dict = {"joined_on": {"$lte": end_date}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": start_date, "$lte": end_date}
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present2": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                                                {
                                                    "$in": [
                                                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}},
                                                        [1, 2, 3, 4, 5],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 1, 2]},
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                            ]
                        },
                    }
                },
                "total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": ["$$value", {"$ifNull": ["$$this.late_minutes", 0]}]
                        },
                    }
                },
                "leave_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$eq": ["$$this.status", STATUS_LEAVE]}, 1, 0]},
                            ]
                        },
                    }
                },
                "on_duty_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$eq": ["$$this.status", STATUS_ON_DUTY]}, 1, 0]},
                            ]
                        },
                    }
                },
                "work_hundredths_sum": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                                                {"$ne": ["$$this.work_hours", None]},
                                            ]
                                        },
                                        {
                                            "$toLong": {
                                                "$round": [{"$multiply": ["$$this.work_hours", 100]}, 0]
                                            }
                                        },
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "work_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                                                {"$ne": ["$$this.work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present2": {"$sum": "$present2"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_hundredths_sum": {"$sum": "$work_hundredths_sum"},
                "work_count": {"$sum": "$work_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]



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



# --------------------------------------------------------------------------- #
# 9. Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee; enforces uniqueness via unique index, returning 409 on duplicate."""
    created_dt = now_dt()
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": created_dt,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees paginated and sorted by emp_code ascending."""
    q: dict = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_employee(emp) for emp in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# 10. Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record punch-in; creates day's attendance document; race-safe via (emp_code, date) unique index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee not found")

    in_dt = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    rec_date = attendance_date(in_dt, emp["shift_start"], emp["shift_end"])
    derived_vals = derive(body.status, in_dt, None, emp["shift_start"], emp["shift_end"], rec_date)

    doc = {
        "emp_code": body.emp_code,
        "date": rec_date,
        "status": body.status,
        "punch_in": in_dt,
        "punch_out": None,
        "work_hours": derived_vals["work_hours"],
        "late_minutes": derived_vals["late_minutes"],
        "overtime_minutes": derived_vals["overtime_minutes"],
        "half_day": derived_vals["half_day"],
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return serialize_record(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Close employee's most recent open record with punch_in <= punched_at."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee not found")

    out_dt = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()

    # Find most recent record with punch_in <= out_dt
    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": out_dt, "$ne": None},
        },
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no punch-in found")

    # Ambiguity A1: if already closed, return 409 directly (do not fall back to older record)
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="record is already punched out")

    # Ambiguity A2 / Rule R4: time validations
    if out_dt <= rec["punch_in"]:
        raise unprocessable(("body", "punched_at"), "punched_at must be strictly greater than punch_in")

    if int((out_dt - rec["punch_in"]).total_seconds()) > MAX_PUNCH_SPAN_SECONDS:
        raise unprocessable(("body", "punched_at"), "punched_at cannot be more than 24 hours after punch_in")

    derived_vals = derive(
        rec["status"],
        rec["punch_in"],
        out_dt,
        emp["shift_start"],
        emp["shift_end"],
        rec["date"],
    )

    update_res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": derived_vals["work_hours"],
                "late_minutes": derived_vals["late_minutes"],
                "overtime_minutes": derived_vals["overtime_minutes"],
                "half_day": derived_vals["half_day"],
            }
        },
    )

    if update_res.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="record was already closed concurrently")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records with database-side sorting and pagination."""
    if date_from is not None:
        parse_date_strict(date_from, ("query", "date_from"))
    if date_to is not None:
        parse_date_strict(date_to, ("query", "date_to"))
    if date_from is not None and date_to is not None:
        if date_from > date_to:
            raise unprocessable(("query", "date_from"), "date_from cannot be after date_to")

    if status_filter is not None and status_filter not in ALL_STATUSES:
        raise unprocessable(("query", "status"), f"status must be one of {ALL_STATUSES}")

    q = build_attendance_list_query(emp_code, date_from, date_to, status_filter)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_record(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    """Regularize an attendance record: recompute derived fields and append to history audit trail."""
    parse_date_strict(date, ("path", "date"))

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee not found")

    old_rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old_rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attendance record not found")

    new_status = body.status if body.status is not None else old_rec["status"]

    if new_status in (STATUS_ABSENT, STATUS_LEAVE):
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(
                ("body", "punch_in"),
                f"punch_in and punch_out cannot be supplied for {new_status} status",
            )
        new_in = None
        new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_rec.get("punch_in")
        if new_in is None:
            raise unprocessable(("body", "punch_in"), f"punch_in is required for status {new_status}")

        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise unprocessable(("body", "punch_in"), "punch_in does not fall on the record's attendance date")

        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_rec.get("punch_out")
        if new_out is not None:
            if new_out <= new_in:
                raise unprocessable(("body", "punch_out"), "punch_out must be strictly greater than punch_in")
            if int((new_out - new_in).total_seconds()) > MAX_PUNCH_SPAN_SECONDS:
                raise unprocessable(("body", "punch_out"), "punch_out cannot be more than 24 hours after punch_in")

    derived_vals = derive(
        new_status,
        new_in,
        new_out,
        emp["shift_start"],
        emp["shift_end"],
        date,
    )

    # Compare with legacy defaults
    old_status = old_rec["status"]
    old_in = old_rec.get("punch_in")
    old_out = old_rec.get("punch_out")
    old_wh = old_rec.get("work_hours")
    old_late = old_rec.get("late_minutes", 0)
    old_ot = old_rec.get("overtime_minutes", 0)
    old_half = old_rec.get("half_day", False)

    new_wh = derived_vals["work_hours"]
    new_late = derived_vals["late_minutes"]
    new_ot = derived_vals["overtime_minutes"]
    new_half = derived_vals["half_day"]

    changes: dict = {}
    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_in != new_in:
        changes["punch_in"] = {"from": old_in, "to": new_in}
    if old_out != new_out:
        changes["punch_out"] = {"from": old_out, "to": new_out}
    if old_wh != new_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if old_late != new_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if old_ot != new_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if old_half != new_half:
        changes["half_day"] = {"from": old_half, "to": new_half}

    # Ambiguity A3: request that changes nothing is 422
    if not changes:
        raise unprocessable(("body",), "request changes nothing")

    hist_entry = {
        "at": now_dt(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    set_doc = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_half,
    }

    # Optimistic locking pin (concurrency guard)
    filter_q: dict = {
        "_id": old_rec["_id"],
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
    }
    if old_rec.get("history") is not None:
        filter_q["history"] = {"$size": len(old_rec["history"])}
    else:
        filter_q["history"] = {"$exists": False}

    update_res = db.attendance_logs.update_one(
        filter_q,
        {
            "$set": set_doc,
            "$push": {"history": hist_entry},
        },
    )

    if update_res.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="concurrent update conflict")

    updated = db.attendance_logs.find_one({"_id": old_rec["_id"]})
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# 11. Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(...)):
    """Monthly attendance summary for one employee via MongoDB aggregation."""
    y, m = parse_month_strict(month, ("query", "month"))
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee not found")

    _, last_day = calendar.monthrange(y, m)
    start_date = f"{y:04d}-{m:02d}-01"
    end_date = f"{y:04d}-{m:02d}-{last_day:02d}"

    # Calculate Mon-Fri working days from max(month_start, joined_on) to month_end (R7)
    joined_date_str = emp["joined_on"]
    eff_start_str = max(start_date, joined_date_str)

    if eff_start_str > end_date:
        working_days = 0
    else:
        cur_d = date.fromisoformat(eff_start_str)
        end_d = date.fromisoformat(end_date)
        working_days = 0
        while cur_d <= end_d:
            if cur_d.weekday() < 5:  # Monday to Friday
                working_days += 1
            cur_d += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
    rows = list(db.attendance_logs.aggregate(pipeline))

    if rows:
        row = rows[0]
        present2 = row.get("present2", 0)
        present_days = float(Decimal(present2) / Decimal(2))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = r8((Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100), 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(...),
    department: Optional[str] = Query(None),
):
    """Department summary for a month via aggregation across employees and attendance_logs."""
    y, m = parse_month_strict(month, ("query", "month"))
    _, last_day = calendar.monthrange(y, m)
    start_date = f"{y:04d}-{m:02d}-01"
    end_date = f"{y:04d}-{m:02d}-{last_day:02d}"

    pipeline = build_department_summary_pipeline(start_date, end_date, department)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for r in rows:
        present2 = r.get("present2", 0)
        present_days = float(Decimal(present2) / Decimal(2))
        work_count = r.get("work_count", 0)
        work_sum = r.get("work_hundredths_sum", 0)
        if work_count == 0:
            avg_work = None
        else:
            avg_work = float((Decimal(work_sum) / (Decimal(work_count) * Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": present_days,
            "avg_work_hours": avg_work,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {
        "month": month,
        "items": items,
    }

