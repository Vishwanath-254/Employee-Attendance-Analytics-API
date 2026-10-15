"""
Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (loads .env if present)
"""
import calendar
from datetime import datetime, timedelta, timezone, time
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# IST Timezone (UTC +5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Environment Variables
MONGO_URI = os.getenv("MONGO_URI",  "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def init_db():
    try:
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)])

        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", -1)])
    except Exception as e:
        print(f"Error initializing indexes: {e}")


init_db()


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("1." + "0" * decimals) if decimals > 0 else Decimal("1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    dt_utc = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt_utc.replace(microsecond=0)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_time_str(t_str: str) -> tuple[int, int]:
    h, m = map(int, t_str.split(":"))
    return h, m


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh, sm = parse_time_str(shift_start)
    eh, em = parse_time_str(shift_end)

    is_overnight = (eh < sh) or (eh == sh and em <= sm)
    if is_overnight:
        end_time = time(eh, em)
        if punch_in_ist.time() < end_time:
            prev_day = punch_in_ist.date() - timedelta(days=1)
            return prev_day.isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    punch_in_ist = punch_in_utc.astimezone(IST)
    year, month, day = map(int, date_str.split("-"))
    sh, sm = parse_time_str(shift_start)
    scheduled_start = datetime(year, month, day, sh, sm, tzinfo=IST)

    diff_sec = (punch_in_ist - scheduled_start).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    punch_out_ist = punch_out_utc.astimezone(IST)
    year, month, day = map(int, date_str.split("-"))
    sh, sm = parse_time_str(shift_start)
    eh, em = parse_time_str(shift_end)

    is_overnight = (eh < sh) or (eh == sh and em <= sm)
    if is_overnight:
        scheduled_end = datetime(year, month, day, eh, em, tzinfo=IST) + timedelta(days=1)
    else:
        scheduled_end = datetime(year, month, day, eh, em, tzinfo=IST)

    diff_sec = (punch_out_ist - scheduled_end).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    raw_hours = diff_sec / 3600.0
    return round_half_up(raw_hours, 2)


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    record = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    history_raw = doc.get("history", [])
    if history_raw:
        formatted_history = []
        for h in history_raw:
            entry = {
                "at": datetime_to_epoch_ms(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {},
            }
            raw_changes = h.get("changes", {})
            for field, change in raw_changes.items():
                fr = change.get("from")
                to = change.get("to")
                if field in ["punch_in", "punch_out"]:
                    fr = datetime_to_epoch_ms(fr) if isinstance(fr, datetime) else fr
                    to = datetime_to_epoch_ms(to) if isinstance(to, datetime) else to
                entry["changes"][field] = {"from": fr, "to": to}
            formatted_history.append(entry)
        record["history"] = formatted_history
    return record


def format_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Models & Validation
# --------------------------------------------------------------------------- #
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000


def validate_epoch_ms(v: Optional[int]) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Must be integer epoch milliseconds")
    if v < MIN_EPOCH_MS or v > MAX_EPOCH_MS:
        raise ValueError(f"Epoch millis out of range [{MIN_EPOCH_MS}, {MAX_EPOCH_MS}]")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def check_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    def val_punched_at(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    def val_status(cls, v):
        if v not in ["PRESENT", "WFH", "ON_DUTY"]:
            raise ValueError("Punch-in status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    def val_punched_at(cls, v):
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    def val_times(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    def val_status(cls, v):
        if v is not None and v not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
            raise ValueError("Invalid status")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/")
def root():
    return {
        "message": "Employee Attendance & Analytics API",
        "docs": "/docs",
        "health": "/health"
    }


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database not available")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items_raw = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    items = [format_employee(it) for it in items_raw]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        in_dt = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    date_str = get_attendance_date(in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(in_dt, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    doc.pop("_id", None)
    return format_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        out_dt = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    recent_log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt, "$ne": None}},
        sort=[("punch_in", -1)],
    )

    if not recent_log:
        raise HTTPException(404, "No punch-in found")

    if recent_log.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    in_dt = recent_log["punch_in"]
    if in_dt.tzinfo is None:
        in_dt = in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (out_dt - in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(422, "punch_out must be strictly after punch_in and within 24 hours")

    work_hrs = compute_work_hours(in_dt, out_dt)
    ot_mins = compute_overtime(out_dt, emp["shift_start"], emp["shift_end"], recent_log["date"])
    is_half = work_hrs < 4.50

    updated_log = db.attendance_logs.find_one_and_update(
        {"_id": recent_log["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": is_half,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_log:
        raise HTTPException(409, "Record already punched out")

    updated_log.pop("_id", None)
    return format_attendance_record(updated_log)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items_raw = list(
        db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    )
    items = [format_attendance_record(it) for it in items_raw]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, "Attendance record not found")

    new_status = body.status if body.status is not None else log["status"]

    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        eff_in_dt = None
        eff_out_dt = None
    else:
        if body.punch_in is not None:
            eff_in_dt = epoch_ms_to_datetime_utc(body.punch_in)
        else:
            eff_in_dt = log.get("punch_in")
            if isinstance(eff_in_dt, datetime) and eff_in_dt.tzinfo is None:
                eff_in_dt = eff_in_dt.replace(tzinfo=timezone.utc)

        if eff_in_dt is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        calc_date = get_attendance_date(eff_in_dt, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            eff_out_dt = epoch_ms_to_datetime_utc(body.punch_out)
        else:
            eff_out_dt = log.get("punch_out")
            if isinstance(eff_out_dt, datetime) and eff_out_dt.tzinfo is None:
                eff_out_dt = eff_out_dt.replace(tzinfo=timezone.utc)

        if eff_out_dt is not None:
            diff_sec = (eff_out_dt - eff_in_dt).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(422, "punch_out must be strictly after punch_in and within 24 hours")

    if new_status in ["ABSENT", "LEAVE"]:
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(eff_in_dt, emp["shift_start"], date)
        if eff_out_dt is not None:
            new_work_hours = compute_work_hours(eff_in_dt, eff_out_dt)
            new_overtime_minutes = compute_overtime(eff_out_dt, emp["shift_start"], emp["shift_end"], date)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    changes = {}

    def add_change(field: str, old_v: Any, new_v: Any):
        if old_v != new_v:
            changes[field] = {"from": old_v, "to": new_v}

    add_change("status", log.get("status"), new_status)
    add_change("punch_in", log.get("punch_in"), eff_in_dt)
    add_change("punch_out", log.get("punch_out"), eff_out_dt)
    add_change("work_hours", log.get("work_hours"), new_work_hours)
    add_change("late_minutes", log.get("late_minutes", 0), new_late_minutes)
    add_change("overtime_minutes", log.get("overtime_minutes", 0), new_overtime_minutes)
    add_change("half_day", log.get("half_day", False), new_half_day)

    if not changes:
        raise HTTPException(422, "No fields changed")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = log.get("history", [])

    updated_log = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"], "history": current_history},
        {
            "$set": {
                "status": new_status,
                "punch_in": eff_in_dt,
                "punch_out": eff_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_log:
        raise HTTPException(409, "Concurrent modification detected")

    updated_log.pop("_id", None)
    return format_attendance_record(updated_log)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year_int, month_int = map(int, month.split("-"))
    num_days = calendar.monthrange(year_int, month_int)[1]

    first_day_month = f"{month}-01"
    last_day_month = f"{month}-{num_days:02d}"

    joined_on = emp["joined_on"]
    start_date_str = max(first_day_month, joined_on)
    end_date_str = last_day_month

    working_days = 0
    if start_date_str <= end_date_str:
        cur = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_d = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        while cur <= end_d:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$regex": f"^{month}-"}
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            }
        }}
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        present_days = stats.get("present_days", 0.0)
        leave_days = stats.get("leave_days", 0)
        late_count = stats.get("late_count", 0)
        total_late_minutes = stats.get("total_late_minutes", 0)
        total_overtime_minutes = stats.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

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


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year_int, month_int = map(int, month.split("-"))
    num_days = calendar.monthrange(year_int, month_int)[1]
    last_day_month = f"{month}-{num_days:02d}"

    emp_match = {"joined_on": {"$lte": last_day_month}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"}
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"codes": "$emp_codes"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$in": ["$emp_code", "$$codes"]},
                    "date": {"$regex": f"^{month}-"}
                }}
            ],
            "as": "logs"
        }},
        {"$project": {
            "department": "$_id",
            "_id": 0,
            "headcount": 1,
            "present_days": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {
                            "$cond": [
                                {"$and": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]}
                                ]},
                                {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                0.0
                            ]
                        }
                    }
                }
            },
            "valid_work_hours": {
                "$filter": {
                    "input": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]}
                                    ]},
                                    "$$l.work_hours",
                                    None
                                ]
                            }
                        }
                    },
                    "as": "wh",
                    "cond": {"$ne": ["$$wh", None]}
                }
            },
            "late_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}
                    }
                }
            },
            "total_late_minutes": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$ifNull": ["$$l.late_minutes", 0]}
                    }
                }
            },
            "leave_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
                    }
                }
            },
            "on_duty_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
                    }
                }
            }
        }},
        {"$project": {
            "department": 1,
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                    {"$avg": "$valid_work_hours"},
                    None
                ]
            },
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1
        }},
        {"$sort": {"department": 1}}
    ]

    items_raw = list(db.employees.aggregate(pipeline))
    items = []
    for it in items_raw:
        it["avg_work_hours"] = round_half_up(it["avg_work_hours"], 2)
        items.append(it)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    pipeline = [
        {"$match": {
            "date": {"$regex": f"^{month}-"},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    if to_date < from_date:
        raise HTTPException(422, "'to' date cannot be earlier than 'from' date")

    d_from = datetime.strptime(from_date, "%Y-%m-%d").date()
    d_to = datetime.strptime(to_date, "%Y-%m-%d").date()
    days_diff = (d_to - d_from).days + 1
    if days_diff > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(404, "Department not found")

    from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_dt_next = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)

    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date": from_dt}},
        {"$densify": {
            "field": "date",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt_next]
            }
        }},
        {"$project": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "day_of_week": {"$dayOfWeek": "$date"}
        }},
        {"$project": {
            "date_str": 1,
            "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"d_str": "$date_str"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$d_str"]}}}
            ],
            "as": "eligible_emps"
        }},
        {"$project": {
            "date_str": 1,
            "is_working_day": 1,
            "headcount": {"$size": "$eligible_emps"}
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d_str": "$date_str"},
            "pipeline": [
                {"$match": {
                    "emp_code": {"$in": emp_codes},
                    "$expr": {"$eq": ["$date", "$$d_str"]}
                }}
            ],
            "as": "day_logs"
        }},
        {"$project": {
            "date_str": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {
                "$sum": {
                    "$map": {
                        "input": "$day_logs",
                        "as": "l",
                        "in": {
                            "$cond": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                0.0
                            ]
                        }
                    }
                }
            },
            "late_count": {
                "$sum": {
                    "$map": {
                        "input": "$day_logs",
                        "as": "l",
                        "in": {
                            "$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]
                        }
                    }
                }
            }
        }},
        {"$project": {
            "date": "$date_str",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, "current"]
                    }
                }
            }
        }},
        {"$sort": {"date": 1}}
    ]

    items_raw = list(db.employees.aggregate(pipeline))
    items = []
    for r in items_raw:
        r.pop("_id", None)
        r["attendance_rate"] = round_half_up(r["attendance_rate"], 4)
        r["moving_avg_7d"] = round_half_up(r["moving_avg_7d"], 4)
        items.append(r)

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1),
    page_size: Optional[int] = Query(20),
):
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"Endpoint must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
        collection_name = "attendance_logs"
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size if page else 0,
                "limit": page_size or 20,
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month parameters are required")
        collection_name = "attendance_logs"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
            {"$group": {"_id": None, "total_late": {"$sum": "$late_minutes"}}},
        ]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month parameter is required")
        collection_name = "employees"
        year_int, month_int = map(int, month.split("-"))
        num_days = calendar.monthrange(year_int, month_int)[1]
        last_day_month = f"{month}-{num_days:02d}"
        emp_match = {"joined_on": {"$lte": last_day_month}}
        if department:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month parameter is required")
        collection_name = "attendance_logs"
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late": {"$sum": "$late_minutes"}}},
        ]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to parameters are required")
        collection_name = "employees"
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
        ]
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)

    return {"endpoint": endpoint, "collection": collection_name, "explain": explain_res}
