import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal

from bson import ObjectId, json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True,
                     tzinfo=UTC, serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def ensure_indexes():
    db.employees.create_index("emp_code", unique=True, name="uq_employee_emp_code")
    db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)],
                              name="department_joined_employee")
    db.employees.create_index([("joined_on", 1), ("department", 1), ("emp_code", 1)],
                              name="joined_department_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], unique=True,
                                    name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1), ("status", 1)],
                                    name="attendance_list")
    db.attendance_logs.create_index([("date", 1), ("late_minutes", -1), ("emp_code", 1)],
                                    name="attendance_analytics")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: date

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: EpochMillis | None = None
    status: PresenceStatus = "PRESENT"

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: EpochMillis | None = None

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def reject_explicit_null_timestamps(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        for field in ("punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


def trunc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def now_utc() -> datetime:
    return trunc(datetime.now(UTC))


def from_ms(value: int | None) -> datetime:
    return now_utc() if value is None else trunc(datetime.fromtimestamp(value / 1000, UTC))


def to_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)


def half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def work_hours(start: datetime, end: datetime) -> float:
    return half_up(Decimal(str((end - start).total_seconds())) / Decimal(3600), 2)


def shift_instant(day: str, value: str, next_day: bool = False) -> datetime:
    d = date.fromisoformat(day) + (timedelta(days=1) if next_day else timedelta())
    return datetime.combine(d, time.fromisoformat(value), IST).astimezone(UTC)


def attendance_day(punch: datetime, shift_start: str, shift_end: str) -> str:
    local = punch.astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def late_minutes(punch: datetime, day: str, shift_start: str) -> int:
    seconds = int((punch - shift_instant(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def overtime_minutes(punch: datetime, day: str, shift_start: str, shift_end: str) -> int:
    end = shift_instant(day, shift_end, shift_end <= shift_start)
    minutes = int((punch - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derive(status: str, punch_in: datetime | None, punch_out: datetime | None, day: str,
           shift_start: str, shift_end: str) -> dict[str, Any]:
    if status not in PRESENCE:
        return {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        raise HTTPException(422, "a presence status requires punch_in")
    if attendance_day(punch_in, shift_start, shift_end) != day:
        raise HTTPException(422, "punch_in does not belong to this attendance date")
    late = late_minutes(punch_in, day, shift_start)
    if punch_out is None:
        return {"punch_in": punch_in, "punch_out": None, "work_hours": None,
                "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    duration = punch_out - punch_in
    if duration <= timedelta() or duration > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    hours = work_hours(punch_in, punch_out)
    return {"punch_in": punch_in, "punch_out": punch_out, "work_hours": hours,
            "late_minutes": late,
            "overtime_minutes": overtime_minutes(punch_out, day, shift_start, shift_end),
            "half_day": hours < 4.5}


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_ms(value)
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def attendance_json(doc: dict[str, Any]) -> dict[str, Any]:
    result = dict(doc)
    for key, default in (("punch_in", None), ("punch_out", None), ("work_hours", None),
                         ("late_minutes", 0), ("overtime_minutes", 0),
                         ("half_day", False), ("history", [])):
        result.setdefault(key, default)
    return json_value(result)


def month_bounds(month: str) -> tuple[str, str, str]:
    try:
        start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise HTTPException(422, "month must be YYYY-MM") from exc
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.isoformat(), next_month.isoformat(), (next_month - timedelta(days=1)).isoformat()


def round_expr(expr: Any, places: int) -> dict[str, Any]:
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5]}}, factor]}


def weekday_expr(expr: Any) -> dict[str, Any]:
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": expr}}}, [2, 3, 4, 5, 6]]}


def attendance_query(emp_code: str | None, date_from: date | None, date_to: date | None,
                     status: Status | None) -> dict[str, Any]:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    if status:
        query["status"] = status
    return query


def monthly_pipeline(code: str, month: str) -> list[dict[str, Any]]:
    start, next_month, end = month_bounds(month)
    return [
        {"$match": {"emp_code": code}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
                                                {"$gte": ["$date", start]}, {"$lt": ["$date", next_month]}]}}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, weekday_expr("$date")]},
                                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
        ], "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$first": "$stats"}, {}]},
                  "work_start": {"$cond": [{"$gt": ["$joined_on", start]}, "$joined_on", start]},
                  "period_end": end}},
        {"$set": {"working_days": {"$cond": [{"$gt": ["$work_start", "$period_end"]}, 0,
            {"$size": {"$filter": {"input": {"$range": [0, {"$add": [{"$dateDiff": {
                "startDate": {"$dateFromString": {"dateString": "$work_start"}},
                "endDate": {"$dateFromString": {"dateString": "$period_end"}}, "unit": "day"}}, 1]}]},
                "as": "offset", "cond": {"$in": [{"$dayOfWeek": {"$dateAdd": {
                    "startDate": {"$dateFromString": {"dateString": "$work_start"}}, "unit": "day",
                    "amount": "$$offset"}}}, [2, 3, 4, 5, 6]]}}}}]}}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            "present_days": {"$ifNull": ["$stats.present_days", 0]},
            "leave_days": {"$ifNull": ["$stats.leave_days", 0]},
            "late_count": {"$ifNull": ["$stats.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$stats.total_late_minutes", 0]},
            "total_overtime_minutes": {"$ifNull": ["$stats.total_overtime_minutes", 0]},
            "attendance_pct": {"$cond": [{"$eq": ["$working_days", 0]}, None,
                round_expr({"$multiply": [{"$divide": [{"$ifNull": ["$stats.present_days", 0]}, "$working_days"]}, 100]}, 2)]}}}
    ]


def department_pipeline(month: str, department: str | None) -> list[dict[str, Any]]:
    start, next_month, end = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
                                                {"$gte": ["$date", start]}, {"$lt": ["$date", next_month]}]}}}
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, weekday_expr("$logs.date")]},
                                                    {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]},
                {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$codes"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$eq": ["$avg_work_hours", None]}, None, round_expr("$avg_work_hours", 2)]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


def leaderboard_pipeline(month: str, limit: int, department: str | None) -> list[dict[str, Any]]:
    start, next_month, _ = month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lt": next_month}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    return pipeline


def trend_pipeline(department: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
    start, days = start_date.isoformat(), (end_date - start_date).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {"employees": 1, "dates": {"$map": {"input": {"$range": [0, days]}, "as": "offset",
            "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start}},
                                  "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$dates"},
        {"$set": {"date": {"$dateToString": {"date": "$dates", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"target": "$date", "codes": "$employees.emp_code"},
          "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$target"]},
                                                            {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}],
          "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$first": "$stats"}, {}]}}},
        {"$set": {"headcount": {"$size": {"$filter": {"input": "$employees", "as": "employee",
                    "cond": {"$lte": ["$$employee.joined_on", "$date"]}}}},
                  "is_working_day": {"$in": [{"$dayOfWeek": "$dates"}, [2, 3, 4, 5, 6]]},
                  "present_count": {"$ifNull": ["$stats.present_count", 0]},
                  "late_count": {"$ifNull": ["$stats.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                                   round_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [
                          {"$eq": ["$moving_raw", None]}, None, round_expr("$moving_raw", 4)]}}}
    ]


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"], doc["created_at"] = body.joined_on.isoformat(), now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return json_value(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Annotated[int, Query(ge=1)] = 1,
                   page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    query = {"department": department} if department else {}
    docs = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1)
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": json_value(docs), "total": db.employees.count_documents(query),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched = from_ms(body.punched_at)
    day = attendance_day(punched, employee["shift_start"], employee["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": punched,
           "punch_out": None, "work_hours": None,
           "late_minutes": late_minutes(punched, day, employee["shift_start"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return attendance_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched = from_ms(body.punched_at)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code,
        "punch_in": {"$ne": None, "$lte": punched}}, sort=[("punch_in", -1)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    values = derive(record["status"], record["punch_in"], punched, record["date"],
                    employee["shift_start"], employee["shift_end"])
    updated = db.attendance_logs.find_one_and_update({"_id": record["_id"], "punch_out": None},
        {"$set": values}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return attendance_json(updated)


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: date | None = None,
                    date_to: date | None = None, status: Status | None = None,
                    page: Annotated[int, Query(ge=1)] = 1,
                    page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    query = attendance_query(emp_code, date_from, date_to, status)
    docs = list(db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [attendance_json(doc) for doc in docs],
            "total": db.attendance_logs.count_documents(query), "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeIn, emp_code: str, attendance_date: Annotated[date, Path(alias="date")]):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    day = attendance_date.isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(404, "attendance record not found")
    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else record["status"]
    if final_status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & supplied):
        raise HTTPException(422, "ABSENT or LEAVE cannot include punch times")
    proposed = {"status": final_status,
                "punch_in": from_ms(body.punch_in) if "punch_in" in supplied else record.get("punch_in"),
                "punch_out": from_ms(body.punch_out) if "punch_out" in supplied else record.get("punch_out")}
    if not any(field in supplied and proposed[field] != record.get(field)
               for field in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "request changes nothing")
    values = derive(proposed["status"], proposed["punch_in"], proposed["punch_out"], day,
                    employee["shift_start"], employee["shift_end"])
    values["status"] = proposed["status"]
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {field: {"from": record.get(field), "to": values[field]}
               for field in fields if record.get(field) != values[field]}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    conditions = [{field: record[field]} if field in record else {field: {"$exists": False}}
                  for field in fields]
    conditions.append({"history": record["history"]} if "history" in record else {"history": {"$exists": False}})
    updated = db.attendance_logs.find_one_and_update({"_id": record["_id"], "$and": conditions},
        {"$set": values, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return attendance_json(updated)


Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    rows = list(db.employees.aggregate(monthly_pipeline(emp_code, month)))
    if not rows:
        raise HTTPException(404, "employee not found")
    return json_value(rows[0])


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: str | None = None):
    return {"month": month, "items": json_value(list(db.employees.aggregate(
        department_pipeline(month, department), allowDiskUse=True)))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: str | None = None):
    return {"month": month, "items": json_value(list(db.attendance_logs.aggregate(
        leaderboard_pipeline(month, limit, department), allowDiskUse=True)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, start: Annotated[date, Query(alias="from")],
                     end: Annotated[date, Query(alias="to")]):
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": json_value(list(db.employees.aggregate(
        trend_pipeline(department, start, end), allowDiskUse=True)))}


ExplainName = Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"]


def aggregate_explain(collection: str, pipeline: list[dict[str, Any]]):
    return db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {},
                                  "allowDiskUse": True},
                      verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: ExplainName, emp_code: str | None = None,
            month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
            department: str | None = None, limit: Annotated[int, Query(ge=1, le=50)] = 10,
            date_from: date | None = None, date_to: date | None = None, status: Status | None = None,
            start: Annotated[date | None, Query(alias="from")] = None,
            end: Annotated[date | None, Query(alias="to")] = None,
            page: Annotated[int, Query(ge=1)] = 1,
            page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    if endpoint == "attendance_list":
        command = {"find": "attendance_logs", "filter": attendance_query(emp_code, date_from, date_to, status),
                   "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        result, collection = db.command("explain", command, verbosity="executionStats"), "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        result, collection = aggregate_explain("employees", monthly_pipeline(emp_code, month)), "employees"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        result, collection = aggregate_explain("employees", department_pipeline(month, department)), "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        result, collection = aggregate_explain("attendance_logs", leaderboard_pipeline(month, limit, department)), "attendance_logs"
    else:
        if not department or start is None or end is None:
            raise HTTPException(422, "department, from and to are required")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        result, collection = aggregate_explain("employees", trend_pipeline(department, start, end)), "employees"
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(result))}
