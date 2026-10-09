"""Employee Attendance & Analytics API.

MongoDB stores instants as UTC-naive BSON datetimes; API instants use epoch milliseconds.
"""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Time, validation, serialization, and calculations
# ---------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_db_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(UTC).replace(tzinfo=None, microsecond=0)


def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "timestamp must be epoch milliseconds")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "invalid epoch-millisecond timestamp")


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    return parsed


def parse_shift_time(value: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", value
    ):
        raise HTTPException(422, "shift time must be HH:MM")
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def month_dates(month: str) -> tuple[date, date, str, str]:
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        first = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")
    last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    return first, last, first.isoformat(), last.isoformat()


def weekdays_between(first: date, last: date) -> int:
    if first > last:
        return 0
    total = 0
    day = first
    while day <= last:
        if day.weekday() < 5:
            total += 1
        day += timedelta(days=1)
    return total


def shift_bounds(day: date, emp: dict) -> tuple[datetime, datetime]:
    start_t = parse_shift_time(emp["shift_start"])
    end_t = parse_shift_time(emp["shift_end"])
    start = datetime.combine(day, start_t, IST)
    end_day = day + timedelta(days=1) if end_t <= start_t else day
    end = datetime.combine(end_day, end_t, IST)
    return start, end


def compute_derived(
    emp: dict,
    attendance_date: str,
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
) -> dict:
    """Return all computed attendance fields for a proposed record."""
    if status not in STATUSES:
        raise HTTPException(422, "invalid status")

    if status in {"ABSENT", "LEAVE"}:
        if punch_in is not None or punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE records cannot have punch times")
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    if punch_in is None:
        raise HTTPException(422, "presence statuses require punch_in")

    punch_in = to_db_datetime(punch_in)
    punch_out = to_db_datetime(punch_out)
    day = parse_date(attendance_date)
    scheduled_start, scheduled_end = shift_bounds(day, emp)

    punch_in_local = punch_in.replace(tzinfo=UTC).astimezone(IST)
    # For an overnight shift, a punch before the shift end can belong to
    # the preceding shift's calendar date.
    if parse_shift_time(emp["shift_end"]) <= parse_shift_time(emp["shift_start"]):
        previous_start, previous_end = shift_bounds(day - timedelta(days=1), emp)
        if previous_start <= punch_in_local < previous_end:
            scheduled_start, scheduled_end = previous_start, previous_end

    late_elapsed = (punch_in_local - scheduled_start).total_seconds()
    late_minutes = int(late_elapsed // 60) if late_elapsed > 600 else 0
    late_minutes = max(0, late_minutes)

    work_hours = None
    overtime_minutes = 0
    half_day = False
    if punch_out is not None:
        if punch_out <= punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (punch_out - punch_in).total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours")
        work_hours = round_half_up((punch_out - punch_in).total_seconds() / 3600, 2)
        half_day = work_hours < 4.50
        punch_out_local = punch_out.replace(tzinfo=UTC).astimezone(IST)
        overtime_elapsed = (punch_out_local - scheduled_end).total_seconds()
        if overtime_elapsed >= 1800:
            overtime_minutes = int(overtime_elapsed // 60)

    return {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def public_doc(doc: dict) -> dict:
    result = dict(doc)
    result.pop("_id", None)
    for key in ("punch_in", "punch_out", "created_at"):
        if isinstance(result.get(key), datetime):
            result[key] = to_epoch_ms(result[key])
    for entry in result.get("history", []) or []:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_epoch_ms(entry["at"])
        changes = entry.get("changes")
        if isinstance(changes, dict):
            for key in ("punch_in", "punch_out"):
                if isinstance(changes.get(key), datetime):
                    changes[key] = to_epoch_ms(changes[key])
    return result


def require_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def create_indexes() -> None:
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employee_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="department_joined_on",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="attendance_employee_date_status",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING)],
        name="attendance_date_late",
    )


@app.on_event("startup")
def startup() -> None:
    create_indexes()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    department: str = Field(min_length=1, max_length=100)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Health and employee endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    parse_date(body.joined_on)
    parse_shift_time(body.shift_start)
    parse_shift_time(body.shift_end)
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    if not body.name.strip() or not body.department.strip():
        raise HTTPException(422, "name and department cannot be blank")
    # Basic email validation without requiring an extra package.
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", body.email):
        raise HTTPException(422, "invalid email")
    doc = body.model_dump()
    doc["name"] = doc["name"].strip()
    doc["department"] = doc["department"].strip()
    doc["created_at"] = to_db_datetime(now_utc())
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": list(cursor), "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    if body.status not in PRESENCE:
        raise HTTPException(422, "status must be PRESENT, WFH, or ON_DUTY")
    instant = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()
    instant_db = to_db_datetime(instant)
    attendance_day = instant.astimezone(IST).date().isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        **compute_derived(emp, attendance_day, body.status, instant_db, None),
        "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    doc["_id"] = result.inserted_id
    return public_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    require_employee(body.emp_code)
    instant = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()
    instant_db = to_db_datetime(instant)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": instant_db},
            "punch_out": None,
            "status": {"$in": list(PRESENCE)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        closed = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": instant_db},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if closed:
            raise HTTPException(409, "already punched out")
        raise HTTPException(404, "no open punch-in found")
    derived = compute_derived(
        require_employee(body.emp_code),
        record["date"],
        record.get("status", "PRESENT"),
        record["punch_in"],
        instant_db,
    )
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": derived},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record updated concurrently")
    return public_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            parse_date(date_from)
            query["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to)
            query["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not exceed date_to")
    if status:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [public_doc(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = require_employee(emp_code)
    attendance_day = parse_date(date).isoformat()
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_day})
    if not old:
        raise HTTPException(404, "attendance record not found")

    status = body.status if body.status is not None else old.get("status", "PRESENT")
    if status not in STATUSES:
        raise HTTPException(422, "invalid status")

    punch_in = (
        from_epoch_ms(body.punch_in)
        if body.punch_in is not None
        else old.get("punch_in")
    )
    punch_out = (
        from_epoch_ms(body.punch_out)
        if body.punch_out is not None
        else old.get("punch_out")
    )

    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have supplied punch times")
        punch_in = punch_out = None
    else:
        if punch_in is None:
            raise HTTPException(422, "presence statuses require punch_in")
        local_day = punch_in.replace(tzinfo=UTC).astimezone(IST).date().isoformat()
        if local_day != attendance_day:
            raise HTTPException(422, "punch_in must fall on attendance date")

    computed = compute_derived(emp, attendance_day, status, punch_in, punch_out)
    changes = {}
    for key, value in (
        ("status", status),
        ("punch_in", computed["punch_in"]),
        ("punch_out", computed["punch_out"]),
    ):
        if value != old.get(key):
            changes[key] = value
    if not changes:
        raise HTTPException(422, "regularization would not change the record")

    history = list(old.get("history") or [])
    history.append(
        {
            "at": to_db_datetime(now_utc()),
            "by": body.regularized_by.strip(),
            "reason": body.reason.strip(),
            "changes": changes,
        }
    )
    old_history = old.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": old["_id"], "history": old_history},
        {"$set": {**computed, "status": status, "history": history}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return public_doc(updated)


# ---------------------------------------------------------------------------
# Analytics: employee monthly
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    emp = require_employee(emp_code)
    first, last, lo, hi = month_dates(month)
    joined = parse_date(emp["joined_on"])
    active_start = max(first, joined)
    working_days = weekdays_between(active_start, last) if active_start <= last else 0

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": lo, "$lte": hi}}},
        {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$status", list(PRESENCE)]},
                                {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                            ]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": ["$status", "LEAVE"]},
                                {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                            ]},
                            1, 0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    row = next(iter(db.attendance_logs.aggregate(pipeline)), {})
    present_days = round_half_up(row.get("present_days", 0))
    attendance_pct = round_half_up((present_days / working_days) * 100, 4) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# Analytics: department summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-\d{2}$")):
    _, last, lo, hi = month_dates(month)
    month_end_dt = datetime.combine(last, time.max)
    pipeline = [
        {"$addFields": {"joined_dt": {"$dateFromString": {"dateString": "$joined_on"}}}},
        {"$match": {"joined_dt": {"$lte": month_end_dt}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", lo]},
                        {"$lte": ["$date", hi]},
                    ]}}},
                    {"$group": {
                        "_id": None,
                        "present_days": {"$sum": {"$cond": [
                            {"$in": ["$status", list(PRESENCE)]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]}},
                        "hours_sum": {"$sum": {"$cond": [
                            {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]},
                            "$work_hours", 0]}},
                        "hours_n": {"$sum": {"$cond": [
                            {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]},
                            1, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    }},
                ],
                "as": "stats",
            }
        },
        {"$addFields": {
            "stats0": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$stats0.present_days", 0]}},
            "hours_sum": {"$sum": {"$ifNull": ["$stats0.hours_sum", 0]}},
            "hours_n": {"$sum": {"$ifNull": ["$stats0.hours_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$stats0.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$stats0.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$stats0.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$stats0.on_duty_count", 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": {"$round": ["$present_days", 2]},
            "avg_work_hours": {"$cond": [
                {"$gt": ["$hours_n", 0]},
                {"$round": [{"$divide": ["$hours_sum", "$hours_n"]}, 2]},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": ASCENDING}},
    ]
    return {"month": month, "items": list(db.employees.aggregate(pipeline))}


# ---------------------------------------------------------------------------
# Analytics: late leaderboard
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None,
):
    _, _, lo, hi = month_dates(month)
    pipeline = [
        {"$match": {"date": {"$gte": lo, "$lte": hi}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "rank": 1,
        }},
    ])
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


# ---------------------------------------------------------------------------
# Analytics: daily department trend, including days with no records
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str,
    date_to: str,
):
    first = parse_date(date_from)
    last = parse_date(date_to)
    if last < first or (last - first).days + 1 > 92:
        raise HTTPException(422, "date range must be ordered and no longer than 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    start_dt = datetime.combine(first, time.min)
    end_dt = datetime.combine(last, time.min)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", list(PRESENCE)]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        {"$addFields": {"_date": {"$dateFromString": {"dateString": "$date"}}}},
        {"$densify": {
            "field": "_date",
            "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]},
        }},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_date"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$_date"}, [1, 7]]}]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$_date"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$addFields": {"joined_dt": {"$dateFromString": {"dateString": "$joined_on"}}}},
                {"$match": {"$expr": {"$lte": ["$joined_dt", "$$day"]}}},
                {"$count": "count"},
            ],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}}},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                None,
            ]}
        }},
        {"$setWindowFields": {
            "sortBy": {"_date": ASCENDING},
            "output": {"moving_avg_7d": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$round": ["$present_count", 2]},
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
        }},
        {"$sort": {"date": ASCENDING}},
    ]
    return {
        "department": department,
        "date_from": first.isoformat(),
        "date_to": last.isoformat(),
        "items": list(db.attendance_logs.aggregate(pipeline)),
    }


# ---------------------------------------------------------------------------
# Explain plans for contract endpoints
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = parse_date(date_from).isoformat()
            if date_to:
                query["date"]["$lte"] = parse_date(date_to).isoformat()
            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "date_from must not exceed date_to")
        return db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        _, _, lo, hi = month_dates(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": lo, "$lte": hi}}}]
        return db.command({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        month_dates(month)
        pipeline = [{"$match": {"department": department}}] if department else []
        pipeline.append({"$group": {"_id": "$department", "n": {"$sum": 1}}})
        return db.command({
            "explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        _, _, lo, hi = month_dates(month)
        pipeline = [{"$match": {"date": {"$gte": lo, "$lte": hi}, "late_minutes": {"$gt": 0}}}]
        return db.command({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })

    if endpoint == "department_trend":
        if not department or not date_from or not date_to:
            raise HTTPException(422, "department, date_from, and date_to are required")
        first, last = parse_date(date_from), parse_date(date_to)
        if last < first or (last - first).days + 1 > 92:
            raise HTTPException(422, "invalid date range")
        pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}]
        return db.command({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })

    raise HTTPException(422, "unsupported explain endpoint")
