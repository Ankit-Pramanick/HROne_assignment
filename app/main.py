"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes initialization
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", 1)], unique=True, name="idx_employees_emp_code_unique")
        db.employees.create_index([("department", 1), ("emp_code", 1)], name="idx_employees_dept_emp_code")
        db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_employees_dept_joined_on")
        db.employees.create_index([("joined_on", 1)], name="idx_employees_joined_on")

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="idx_attendance_emp_date_unique")
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_attendance_date_desc_emp_asc")
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], name="idx_attendance_emp_date_desc")
        db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="idx_attendance_status_date_emp")
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_attendance_emp_punch_in_desc")
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)], name="idx_attendance_date_late_minutes")
    except Exception:
        # Avoid crashing startup if MongoDB is unreachable yet; the health check will signal status
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Rounding and Date / Time Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Any, decimals: int = 2) -> Optional[float]:
    """R8: Rounding of reported numbers: 2 decimals half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def to_utc_dt(val: Any) -> Optional[datetime]:
    """Convert an epoch ms or datetime to a UTC-aware datetime truncated to whole seconds."""
    if val is None:
        return None
    if isinstance(val, int):
        ms_truncated = (val // 1000) * 1000
        return datetime.fromtimestamp(ms_truncated / 1000, tz=timezone.utc)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        else:
            val = val.astimezone(timezone.utc)
        return val.replace(microsecond=0)
    return None


def to_epoch_ms(val: Any) -> Optional[int]:
    """Convert a datetime or epoch ms to integer milliseconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return (int(val) // 1000) * 1000
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return int(val.timestamp() * 1000)
    return None


def to_ist(dt: datetime) -> datetime:
    """Convert datetime to IST timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means an overnight shift."""
    sh_start, sm_start = map(int, shift_start.split(":"))
    sh_end, sm_end = map(int, shift_end.split(":"))
    return (sh_end, sm_end) <= (sh_start, sm_start)


def determine_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = to_ist(punch_in_dt)
    if is_overnight_shift(shift_start, shift_end):
        sh_end, sm_end = map(int, shift_end.split(":"))
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (sh_end, sm_end, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def get_shift_datetimes(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """
    Return shift_start and shift_end as IST datetimes for a given attendance date.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    sh_start, sm_start = map(int, shift_start.split(":"))
    sh_end, sm_end = map(int, shift_end.split(":"))
    base_date = datetime.fromisoformat(date_str).date()
    start_dt = datetime(base_date.year, base_date.month, base_date.day, sh_start, sm_start, 0, tzinfo=IST)
    if is_overnight_shift(shift_start, shift_end):
        end_date = base_date + timedelta(days=1)
        end_dt = datetime(end_date.year, end_date.month, end_date.day, sh_end, sm_end, 0, tzinfo=IST)
    else:
        end_dt = datetime(base_date.year, base_date.month, base_date.day, sh_end, sm_end, 0, tzinfo=IST)
    return start_dt, end_dt


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R2: Grace period is 10 minutes. A punch-in is late only if strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start. Otherwise 0.
    """
    punch_in_ist = to_ist(punch_in_dt)
    start_dt, _ = get_shift_datetimes(date_str, shift_start, shift_end)
    diff_sec = (punch_in_ist - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    val = Decimal(str(diff_sec)) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: Overtime = whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0."""
    punch_out_ist = to_ist(punch_out_dt)
    _, end_dt = get_shift_datetimes(date_str, shift_start, shift_end)
    diff_sec = (punch_out_ist - end_dt).total_seconds()
    if diff_sec <= 0:
        return 0
    diff_min = int(diff_sec // 60)
    return diff_min if diff_min >= 30 else 0


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Working days are Mon-Fri, counted from joined_on for mid-month joiners.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    last_day_str = f"{month_str}-{num_days:02d}"
    first_day_str = f"{month_str}-01"

    if joined_on_str > last_day_str:
        return 0

    start_day = 1
    if joined_on_str > first_day_str:
        start_day = int(joined_on_str.split("-")[2])

    working_days = 0
    for d in range(start_day, num_days + 1):
        if datetime(year, month, d).weekday() < 5:
            working_days += 1
    return working_days


# --------------------------------------------------------------------------- #
# Document Serializers
# --------------------------------------------------------------------------- #
def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field in ("punch_in", "punch_out"):
            from_val = to_epoch_ms(from_val)
            to_val = to_epoch_ms(to_val)
        changes[field] = {
            "from": from_val,
            "to": to_val,
        }
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    """Serialize MongoDB attendance document to HTTP response model."""
    raw_history = doc.get("history")
    history_list = [serialize_history_entry(h) for h in raw_history] if raw_history is not None else []
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_list,
    }


def serialize_employee_doc(doc: dict) -> dict:
    """Serialize MongoDB employee document to HTTP response model."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_mongo(obj: Any) -> Any:
    """Recursively convert BSON / non-JSON objects to JSON-friendly structures."""
    if isinstance(obj, dict):
        return {k: serialize_mongo(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serialize_mongo(v) for v in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif hasattr(obj, "__str__") and type(obj).__name__ in ("ObjectId", "Decimal128"):
        return str(obj)
    return obj


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT")

    @field_validator("status")
    def validate_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("status must be one of PRESENT, ABSENT, LEAVE, WFH, ON_DUTY")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# Root redirect to documentation
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


# 1. Health Probe
@app.get("/health")
def health():
    """Readiness probe: 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"MongoDB ping failed: {str(e)}")


# 2. Create Employee
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """
    Create an employee. Enforces unique emp_code via unique index.
    Server sets created_at as UTC datetime, returns epoch ms.
    """
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_employee_doc(doc)


# 3. List Employees
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending. Total reflects filters."""
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# 4. Punch In
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates the day's record (R1, R2).
    Race-safe via unique compound index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punched_dt = to_utc_dt(body.punched_at)
    else:
        punched_dt = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = determine_attendance_date(punched_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punched_dt, emp["shift_start"], emp["shift_end"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return serialize_attendance_doc(doc)


# 5. Punch Out
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes the employee's most recent record whose punch_in <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3) and half_day (R5).
    Atomic update ensures concurrent punch-outs yield exactly one 200 and the rest 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = to_utc_dt(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in is at or before punched_at
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found for this punch-out")

    if doc.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    punch_in_dt = to_utc_dt(doc["punch_in"])
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be after punch_in and within 24 hours of it",
        )

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], doc["date"])
    half_day_flag = work_hrs < 4.50

    # Atomic update to avoid race conditions
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_min,
                "half_day": half_day_flag,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record was punched out concurrently")

    return serialize_attendance_doc(updated_doc)


# 6. List Attendance
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, enum=["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Streams directly from cursor with index pagination; does not load collection into memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# 7. Regularize Attendance (PATCH)
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    body: RegularizeRequest,
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields, appends exactly one entry to history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found for this date")

    old_status = doc.get("status")
    old_punch_in = doc.get("punch_in")
    old_punch_out = doc.get("punch_out")
    old_work_hours = doc.get("work_hours")
    old_late_minutes = doc.get("late_minutes", 0)
    old_overtime_minutes = doc.get("overtime_minutes", 0)
    old_half_day = doc.get("half_day", False)
    old_history = doc.get("history") or []

    target_status = body.status if body.status is not None else old_status

    # Validate status and punch times rules
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in and punch_out cannot be supplied when setting status to ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = to_utc_dt(body.punch_in)
        else:
            new_punch_in = old_punch_in

        if new_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A presence status requires a punch_in",
            )

        # R1: punch_in must stay on the record's attendance date
        computed_date = determine_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date",
            )

        if body.punch_out is not None:
            new_punch_out = to_utc_dt(body.punch_out)
        else:
            new_punch_out = old_punch_out

        if new_punch_out is not None:
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in and within 24 hours of it",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"], date)

    # Determine what actually changed
    changes = {}
    if target_status != old_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if to_epoch_ms(new_punch_in) != to_epoch_ms(old_punch_in):
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if to_epoch_ms(new_punch_out) != to_epoch_ms(old_punch_out):
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A request that changes nothing is 422",
        )

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match current history length
    query = {"_id": doc["_id"]}
    if old_history:
        query["history"] = old_history
    else:
        query["$or"] = [{"history": []}, {"history": {"$exists": False}}]

    updated_doc = db.attendance_logs.find_one_and_update(
        query,
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on this attendance record",
        )

    return serialize_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Pipeline Endpoints
# --------------------------------------------------------------------------- #

# 8. Employee Monthly Analytics
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by MongoDB aggregation.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    working_days = count_working_days(emp["joined_on"], month)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [1, 2, 3, 4, 5]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_min = int(row.get("total_late_minutes", 0))
        total_overtime_min = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_min = 0
        total_overtime_min = 0

    if working_days > 0:
        attendance_pct = round_half_up((Decimal(str(present_days)) / Decimal(str(working_days))) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_min,
        "total_overtime_minutes": total_overtime_min,
        "attendance_pct": attendance_pct,
    }


# 9. Department Summary Analytics
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month via MongoDB aggregation across both collections.
    Headcount counts all employees with joined_on <= end of month (even with zero logs).
    """
    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_emp = {"joined_on": {"$lte": end_date}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$emp"]},
                            "date": {"$gte": start_date, "$lte": end_date},
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$isoDayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": ["$$l.day_of_week", [1, 2, 3, 4, 5]]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
                "work_hours_list": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "l",
                        "in": "$$l.work_hours",
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "all_work_hours": {"$push": "$work_hours_list"},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        flat_wh = [wh for sub in r.get("all_work_hours", []) for wh in sub if wh is not None]
        avg_wh = round_half_up(sum(flat_wh) / len(flat_wh), 2) if flat_wh else None
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


# 10. Late Leaderboard
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month using standard competition ranking with ties.
    Limit applies after ranking (rank <= limit).
    """
    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {
            "$match": {
                "emp.0": {"$exists": True}
            }
        },
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": {"$arrayElemAt": ["$emp.name", 0]},
                "department": {"$arrayElemAt": ["$emp.department", 0]},
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])

    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


# 11. Department Daily Trend
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """
    Daily attendance trend for a department.
    Gap filling via $densify and 7-day moving average via $setWindowFields.
    """
    d_from = datetime.fromisoformat(from_date).date()
    d_to = datetime.fromisoformat(to_date).date()

    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' must be >= 'from'")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    # Check department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Department '{department}' not found")

    emp_codes = db.employees.distinct("emp_code", {"department": department})

    start_dt = datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(d_to.year, d_to.month, d_to.day, 0, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        # Guarantee input stream for $densify even if 0 logs exist in the range
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "date": {"$literal": from_date},
                            "status": {"$literal": "__DUMMY__"},
                            "half_day": {"$literal": False},
                            "late_minutes": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$isoDayOfWeek": "$date_dt"},
            }
        },
        {
            "$set": {
                "is_working_day": {"$in": ["$day_of_week", [1, 2, 3, 4, 5]]}
            }
        },
        {"$sort": {"date_dt": 1}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"curr_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$curr_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        rate = r.get("attendance_rate")
        if rate is not None:
            rate = round_half_up(rate, 4)
        mavg = r.get("moving_avg_7d")
        if mavg is not None:
            mavg = round_half_up(mavg, 4)

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": mavg,
        })

    return {
        "department": department,
        "items": items,
    }


# 12. Explain Endpoint
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., enum=["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, enum=["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Runs MongoDB explain with executionStats verbosity for the main query or aggregation
    of the named endpoint, built exactly as that endpoint builds it.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from > date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_mongo(res)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code and month are required")
        year, month_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, month_num)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            },
            {
                "$addFields": {
                    "day_of_week": {
                        "$isoDayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$day_of_week", [1, 2, 3, 4, 5]]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_mongo(res)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        year, month_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, month_num)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        match_emp = {"joined_on": {"$lte": end_date}}
        if department:
            match_emp["department"] = department
        pipeline = [
            {"$match": match_emp},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {"$eq": ["$emp_code", "$$emp"]},
                                "date": {"$gte": start_date, "$lte": end_date},
                            }
                        },
                        {
                            "$addFields": {
                                "day_of_week": {
                                    "$isoDayOfWeek": {
                                        "$dateFromString": {"dateString": "$date"}
                                    }
                                }
                            }
                        },
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "department": 1,
                    "emp_code": 1,
                    "present_days": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$in": ["$$l.day_of_week", [1, 2, 3, 4, 5]]},
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$gt": ["$$l.late_minutes", 0]},
                            }
                        }
                    },
                    "total_late_minutes": {"$sum": "$logs.late_minutes"},
                    "leave_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "LEAVE"]},
                            }
                        }
                    },
                    "on_duty_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                            }
                        }
                    },
                    "work_hours_list": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$present_days"},
                    "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"},
                    "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"},
                    "all_work_hours": {"$push": "$work_hours_list"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "employees", "explain": serialize_mongo(res)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        year, month_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, month_num)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {
                "$match": {
                    "emp.0": {"$exists": True}
                }
            },
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {
                "$project": {
                    "emp_code": "$_id",
                    "name": {"$arrayElemAt": ["$emp.name", 0]},
                    "department": {"$arrayElemAt": ["$emp.department", 0]},
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            {
                "$match": {
                    "rank": {"$lte": limit}
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ])
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_mongo(res)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department, from, and to are required")
        d_from = datetime.fromisoformat(from_date).date()
        d_to = datetime.fromisoformat(to_date).date()
        if d_to < d_from:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' must be >= 'from'")
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

        emp_codes = db.employees.distinct("emp_code", {"department": department})
        start_dt = datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0, tzinfo=timezone.utc)
        end_dt = datetime(d_to.year, d_to.month, d_to.day, 0, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)
        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$unionWith": {
                    "coll": "employees",
                    "pipeline": [
                        {"$match": {"department": department}},
                        {"$limit": 1},
                        {
                            "$project": {
                                "date": {"$literal": from_date},
                                "status": {"$literal": "__DUMMY__"},
                                "half_day": {"$literal": False},
                                "late_minutes": {"$literal": 0},
                            }
                        },
                    ],
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                }
            },
            {
                "$project": {
                    "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_dt],
                    },
                }
            },
            {
                "$set": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                    "day_of_week": {"$isoDayOfWeek": "$date_dt"},
                }
            },
            {
                "$set": {
                    "is_working_day": {"$in": ["$day_of_week", [1, 2, 3, 4, 5]]}
                }
            },
            {"$sort": {"date_dt": 1}},
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"curr_date": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$curr_date"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "hc",
                }
            },
            {
                "$set": {
                    "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_working_day", True]},
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date_dt": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, "current"]
                            },
                        }
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
        ]
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_mongo(res)}

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Unknown endpoint: {endpoint}")
