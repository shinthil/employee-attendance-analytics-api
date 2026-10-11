

import os

from datetime import date as Date, datetime, time, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Literal, Optional



from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query

from pydantic import (

    BaseModel,

    ConfigDict,

    Field,

    StrictInt,

    field_validator,

    model_validator,

)

from pymongo import ASCENDING, DESCENDING, MongoClient

from pymongo.errors import DuplicateKeyError, PyMongoError



load_dotenv()



MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

MONGO_DB = os.getenv("MONGO_DB", "attendance_db")



client = MongoClient(

    MONGO_URI,

    tz_aware=True,

    serverSelectionTimeoutMS=3000,

)

db = client[MONGO_DB]



IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

MIN_EPOCH_MS = 100_000_000_000

MAX_EPOCH_MS = 4_102_444_800_000



PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}



app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")





# ---------------------------------------------------------------------

# Shared helpers

# ---------------------------------------------------------------------



def truncate_to_seconds(value: datetime) -> datetime:

    if value.tzinfo is None:

        value = value.replace(tzinfo=UTC)

    return value.replace(microsecond=0)





def as_utc(value: datetime) -> datetime:

    if value.tzinfo is None:

        value = value.replace(tzinfo=UTC)

    return value.astimezone(UTC)





def epoch_ms(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return int(as_utc(value).timestamp() * 1000)


def parse_shift(value: str) -> time:

    try:

        return time.fromisoformat(value)

    except (TypeError, ValueError) as exc:

        raise ValueError("shift times must use HH:MM format") from exc





def shift_start_for_punch(

    punch_in: datetime,

    shift_start: str,

    shift_end: str,

) -> datetime:

    local_punch = as_utc(punch_in).astimezone(IST)

    start_time = parse_shift(shift_start)

    end_time = parse_shift(shift_end)



    start_day = local_punch.date()

    if (

        end_time <= start_time

        and local_punch.timetz().replace(tzinfo=None) < end_time

    ):

        start_day -= timedelta(days=1)



    return datetime.combine(start_day, start_time, tzinfo=IST)





def compute_late_minutes(

    punch_in: datetime,

    shift_start: str,

    shift_end: str = "18:30",

) -> int:

    punch = as_utc(truncate_to_seconds(punch_in)).astimezone(IST)

    start = shift_start_for_punch(punch, shift_start, shift_end)

    elapsed_seconds = (punch - start).total_seconds()



    if elapsed_seconds <= 10 * 60:

        return 0



    return max(0, int(elapsed_seconds // 60))





def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:

    seconds = Decimal(

        str((as_utc(punch_out) - as_utc(punch_in)).total_seconds())

    )

    hours = seconds / Decimal("3600")

    return float(

        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    )





def compute_overtime(

    punch_out: datetime,

    shift_end: str,

    date_str: str,

    shift_start: str = "09:30",

) -> int:

    try:

        shift_day = Date.fromisoformat(date_str)

        end_time = parse_shift(shift_end)

        start_time = parse_shift(shift_start)

    except (TypeError, ValueError) as exc:

        raise ValueError("invalid date or shift time") from exc



    end_day = (

        shift_day + timedelta(days=1)

        if end_time <= start_time

        else shift_day

    )

    shift_end_instant = datetime.combine(end_day, end_time, tzinfo=IST)



    elapsed_seconds = (

        as_utc(punch_out) - shift_end_instant.astimezone(UTC)

    ).total_seconds()

    overtime = max(0, int(elapsed_seconds // 60))



    return overtime if overtime >= 30 else 0





def attendance_for_response(doc: dict) -> dict:

    result = {

        key: value for key, value in doc.items() if key != "_id"

    }

    result["punch_in"] = epoch_ms(result.get("punch_in"))

    result["punch_out"] = epoch_ms(result.get("punch_out"))



    history = []

    for entry in result.get("history") or []:

        item = dict(entry)

        item["at"] = epoch_ms(item.get("at"))



        changes = {}

        for field, change in (item.get("changes") or {}).items():

            change = dict(change)

            if field in {"punch_in", "punch_out"}:

                change["from"] = epoch_ms(change.get("from"))

                change["to"] = epoch_ms(change.get("to"))

            changes[field] = change



        item["changes"] = changes

        history.append(item)



    result["history"] = history

    result.setdefault("late_minutes", 0)

    result.setdefault("overtime_minutes", 0)

    result.setdefault("half_day", False)

    return result





def employee_for_response(doc: dict) -> dict:

    result = {

        key: value for key, value in doc.items() if key != "_id"

    }

    result["created_at"] = epoch_ms(result.get("created_at"))

    return result





def validate_epoch_ms(value: Optional[int]) -> Optional[int]:

    if value is None:

        return None

    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:

        raise ValueError(

            f"timestamp must be between {MIN_EPOCH_MS} "

            f"and {MAX_EPOCH_MS} milliseconds"

        )

    return value





# ---------------------------------------------------------------------

# Request models

# ---------------------------------------------------------------------



class EmployeeIn(BaseModel):

    model_config = ConfigDict(extra="ignore")



    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(

        max_length=120,

        pattern=r"^[^@\s]+@[^@\s]+**\\.**[^@\s]+$",

    )

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(

        default="09:30",

        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",

    )

    shift_end: str = Field(

        default="18:30",

        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",

    )

    joined_on: Date



    @model_validator(mode="after")

    def validate_shift(self):

        if self.shift_start == self.shift_end:

            raise ValueError("shift_start must differ from shift_end")

        return self





class PunchInIn(BaseModel):

    model_config = ConfigDict(extra="ignore")



    emp_code: str = Field(min_length=1)

    punched_at: Optional[StrictInt] = None

    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"



    @field_validator("punched_at")

    @classmethod

    def validate_punched_at(cls, value):

        return validate_epoch_ms(value)





class PunchOutIn(BaseModel):

    model_config = ConfigDict(extra="ignore")



    emp_code: str = Field(min_length=1)

    punched_at: Optional[StrictInt] = None



    @field_validator("punched_at")

    @classmethod

    def validate_punched_at(cls, value):

        return validate_epoch_ms(value)





class RegularizeIn(BaseModel):

    model_config = ConfigDict(extra="ignore")



    status: Optional[

        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

    ] = None

    punch_in: Optional[StrictInt] = None

    punch_out: Optional[StrictInt] = None

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)



    @field_validator("punch_in", "punch_out")

    @classmethod

    def validate_timestamps(cls, value):

        return validate_epoch_ms(value)





# ---------------------------------------------------------------------

# Startup indexes

# ---------------------------------------------------------------------



@app.on_event("startup")

def create_indexes() -> None:

    db.employees.create_index(

        [("emp_code", ASCENDING)],

        unique=True,

        name="uq_employees_emp_code",

    )

    db.employees.create_index(

        [("department", ASCENDING), ("emp_code", ASCENDING)],

        name="ix_employees_department_code",

    )

    db.attendance_logs.create_index(

        [("emp_code", ASCENDING), ("date", ASCENDING)],

        unique=True,

        name="uq_attendance_emp_date",

    )

    db.attendance_logs.create_index(

        [("date", DESCENDING), ("emp_code", ASCENDING)],

        name="ix_attendance_date_emp",

    )

    db.attendance_logs.create_index(

        [("emp_code", ASCENDING), ("date", DESCENDING)],

        name="ix_attendance_emp_date_desc",

    )

    db.attendance_logs.create_index(

        [

            ("status", ASCENDING),

            ("date", DESCENDING),

            ("emp_code", ASCENDING),

        ],

        name="ix_attendance_status_date_emp",

    )





# ---------------------------------------------------------------------

# Health

# ---------------------------------------------------------------------



@app.get("/health")

def health():

    try:

        db.command("ping")

    except PyMongoError:

        raise HTTPException(

            status_code=503,

            detail="MongoDB unavailable",

        )

    return {"status": "ok"}





# ---------------------------------------------------------------------

# Employee endpoints

# ---------------------------------------------------------------------



@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):

    doc = body.model_dump()

    doc["joined_on"] = doc["joined_on"].isoformat()

    doc["created_at"] = datetime.now(UTC)



    try:

        db.employees.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(

            status_code=409,

            detail="emp_code already exists",

        )

    return employee_for_response(doc)





@app.get("/employees")

def list_employees(

    department: Optional[str] = None,

    page: int = Query(default=1, ge=1),

    page_size: int = Query(default=20, ge=1, le=100),

):

    query = {"department": department} if department is not None else {}

    total = db.employees.count_documents(query)



    cursor = (

        db.employees.find(query, {"_id": 0})

        .sort("emp_code", ASCENDING)

        .skip((page - 1) * page_size)

        .limit(page_size)

    )

    items = [employee_for_response(doc) for doc in cursor]



    return {

        "items": items,

        "total": total,

        "page": page,

        "page_size": page_size,

    }





# ---------------------------------------------------------------------

# Punch-in

# ---------------------------------------------------------------------



@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:

        raise HTTPException(status_code=404, detail="employee not found")



    punched = (

        datetime.now(UTC)

        if body.punched_at is None

        else datetime.fromtimestamp(body.punched_at / 1000, tz=UTC)

    )

    punched = truncate_to_seconds(punched)

    local_punch = punched.astimezone(IST)



    start_time = parse_shift(emp["shift_start"])

    end_time = parse_shift(emp["shift_end"])

    attendance_day = local_punch.date()



    if (

        end_time <= start_time

        and local_punch.timetz().replace(tzinfo=None) < end_time

    ):

        attendance_day -= timedelta(days=1)



    doc = {

        "emp_code": body.emp_code,

        "date": attendance_day.isoformat(),

        "status": body.status,

        "punch_in": punched,

        "punch_out": None,

        "work_hours": None,

        "late_minutes": compute_late_minutes(

            punched, emp["shift_start"], emp["shift_end"]

        ),

        "overtime_minutes": 0,

        "half_day": False,

        "history": [],

    }



    try:

        db.attendance_logs.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(

            status_code=409,

            detail="already punched in for this date",

        )



    return attendance_for_response(doc)





# ---------------------------------------------------------------------

# Punch-out

# ---------------------------------------------------------------------



@app.post("/attendance/punch-out")

def punch_out(body: PunchOutIn):

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:

        raise HTTPException(status_code=404, detail="employee not found")



    punched = (

        datetime.now(UTC)

        if body.punched_at is None

        else datetime.fromtimestamp(body.punched_at / 1000, tz=UTC)

    )

    punched = truncate_to_seconds(punched)



    query = {

        "emp_code": body.emp_code,

        "punch_in": {"$lte": punched},

    }

    doc = db.attendance_logs.find_one(

        query,

        sort=[("punch_in", DESCENDING)],

    )

    if doc is None:

        raise HTTPException(

            status_code=404,

            detail="no punch-in found before punch-out time",

        )



    punch_in_time = as_utc(doc["punch_in"])

    if doc.get("punch_out") is not None:

        raise HTTPException(

            status_code=409,

            detail="attendance record was already punched out",

        )

    if punched <= punch_in_time:

        raise HTTPException(

            status_code=422,

            detail="punch-out must be after punch-in",

        )

    if (punched - punch_in_time).total_seconds() > 24 * 3600:

        raise HTTPException(

            status_code=422,

            detail="punch-out must be within 24 hours of punch-in",

        )



    work_hours = compute_work_hours(punch_in_time, punched)

    overtime_minutes = compute_overtime(

        punched, emp["shift_end"], doc["date"], emp["shift_start"]

    )

    half_day = work_hours < 4.50



    result = db.attendance_logs.update_one(

        {"_id": doc["_id"], "punch_out": None},

        {

            "$set": {

                "punch_out": punched,

                "work_hours": work_hours,

                "overtime_minutes": overtime_minutes,

                "half_day": half_day,

            }

        },

    )

    if result.modified_count != 1:

        raise HTTPException(

            status_code=409,

            detail="attendance record was already punched out",

        )



    updated = db.attendance_logs.find_one(

        {"_id": doc["_id"]},

        {"_id": 0},

    )

    return attendance_for_response(updated)





# ---------------------------------------------------------------------

# Attendance regularization

# ---------------------------------------------------------------------



@app.patch("/attendance/{emp_code}/{date}")

def regularize_attendance(

    emp_code: str,

    date: Date,

    body: RegularizeIn,

):

    date_key = date.isoformat()



    emp = db.employees.find_one({"emp_code": emp_code})

    if emp is None:

        raise HTTPException(status_code=404, detail="employee not found")



    doc = db.attendance_logs.find_one(

        {"emp_code": emp_code, "date": date_key}

    )

    if doc is None:

        raise HTTPException(

            status_code=404,

            detail="attendance record not found",

        )



    requested = body.model_dump(

        exclude_unset=True,

        exclude={"reason", "regularized_by"},

    )

    if not requested:

        raise HTTPException(

            status_code=422,

            detail="no attendance fields supplied",

        )



    new_status = requested.get("status", doc["status"])

    new_in = doc.get("punch_in")

    new_out = doc.get("punch_out")



    if "punch_in" in requested:

        value = requested["punch_in"]

        new_in = (

            datetime.fromtimestamp(value / 1000, tz=UTC)

            if value is not None

            else None

        )

        if new_in is not None:

            new_in = truncate_to_seconds(new_in)



    if "punch_out" in requested:

        value = requested["punch_out"]

        new_out = (

            datetime.fromtimestamp(value / 1000, tz=UTC)

            if value is not None

            else None

        )

        if new_out is not None:

            new_out = truncate_to_seconds(new_out)



    if new_status in {"ABSENT", "LEAVE"}:

        if "punch_in" in requested or "punch_out" in requested:

            raise HTTPException(

                status_code=422,

                detail="ABSENT and LEAVE cannot be supplied with punch times",

            )

        new_in = None

        new_out = None

    elif new_in is None:

        raise HTTPException(

            status_code=422,

            detail="presence status requires punch_in",

        )



    if new_in is not None:

        local_in = as_utc(new_in).astimezone(IST)

        shift_start = parse_shift(emp["shift_start"])

        shift_end = parse_shift(emp["shift_end"])

        attendance_day = local_in.date()



        if (

            shift_end <= shift_start

            and local_in.timetz().replace(tzinfo=None) < shift_end

        ):

            attendance_day -= timedelta(days=1)



        if attendance_day.isoformat() != date_key:

            raise HTTPException(

                status_code=422,

                detail="punch_in must belong to the attendance date",

            )



    if new_out is not None:

        if new_in is None or new_out <= new_in:

            raise HTTPException(

                status_code=422,

                detail="punch_out must be after punch_in",

            )

        if (new_out - new_in).total_seconds() > 24 * 3600:

            raise HTTPException(

                status_code=422,

                detail="punch_out must be within 24 hours of punch_in",

            )



    new_late = (

        compute_late_minutes(new_in, emp["shift_start"], emp["shift_end"])

        if new_in is not None

        else 0

    )



    if new_in is not None and new_out is not None:

        new_hours = compute_work_hours(new_in, new_out)

        new_overtime = compute_overtime(

            new_out, emp["shift_end"], date_key, emp["shift_start"]

        )

        new_half_day = new_hours < 4.50

    else:

        new_hours = None

        new_overtime = 0

        new_half_day = False



    final_values = {

        "status": new_status,

        "punch_in": new_in,

        "punch_out": new_out,

        "work_hours": new_hours,

        "late_minutes": new_late,

        "overtime_minutes": new_overtime,

        "half_day": new_half_day,

    }

    old_values = {

        "status": doc.get("status"),

        "punch_in": doc.get("punch_in"),

        "punch_out": doc.get("punch_out"),

        "work_hours": doc.get("work_hours"),

        "late_minutes": doc.get("late_minutes", 0),

        "overtime_minutes": doc.get("overtime_minutes", 0),

        "half_day": doc.get("half_day", False),

    }



    def comparable(field, value):

        if field in {"punch_in", "punch_out"} and value is not None:

            return epoch_ms(value)

        return value



    changes = {}

    for field, new_value in final_values.items():

        old_value = old_values[field]

        if comparable(field, old_value) != comparable(field, new_value):

            changes[field] = {

                "from": comparable(field, old_value),

                "to": comparable(field, new_value),

            }



    if not changes:

        raise HTTPException(

            status_code=422,

            detail="request does not change the attendance record",

        )



    history_entry = {

        "at": datetime.now(UTC),

        "by": body.regularized_by,

        "reason": body.reason,

        "changes": changes,

    }



    # Match the previously read record so concurrent edits cannot overwrite history.

    result = db.attendance_logs.update_one(

        {

            "_id": doc["_id"],

            "history": doc.get("history", []),

            "status": doc.get("status"),

            "punch_in": doc.get("punch_in"),

            "punch_out": doc.get("punch_out"),

        },

        {

            "$set": final_values,

            "$push": {"history": history_entry},

        },

    )

    if result.modified_count != 1:

        raise HTTPException(

            status_code=409,

            detail="attendance record changed concurrently; retry",

        )



    updated = db.attendance_logs.find_one(

        {"_id": doc["_id"]},

        {"_id": 0},

    )

    return attendance_for_response(updated)





# ---------------------------------------------------------------------

# List attendance

# ---------------------------------------------------------------------



@app.get("/attendance")

def list_attendance(

    emp_code: Optional[str] = None,

    date_from: Optional[Date] = None,

    date_to: Optional[Date] = None,

    status: Optional[

        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

    ] = None,

    page: int = Query(default=1, ge=1),

    page_size: int = Query(default=20, ge=1, le=100),

):

    if date_from and date_to and date_from > date_to:

        raise HTTPException(

            status_code=422,

            detail="date_from must be on or before date_to",

        )



    query = {}



    if emp_code is not None:

        query["emp_code"] = emp_code



    if date_from is not None or date_to is not None:

        query["date"] = {}

        if date_from is not None:

            query["date"]["$gte"] = date_from.isoformat()

        if date_to is not None:

            query["date"]["$lte"] = date_to.isoformat()



    if status is not None:

        query["status"] = status



    total = db.attendance_logs.count_documents(query)



    cursor = (

        db.attendance_logs.find(query, {"_id": 0})

        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])

        .skip((page - 1) * page_size)

        .limit(page_size)

    )

    items = [attendance_for_response(doc) for doc in cursor]



    return {

        "items": items,

        "total": total,

        "page": page,

        "page_size": page_size,

    }

# ---------------------------------------------------------------------
# Analytics helpers and endpoints
# ---------------------------------------------------------------------

def month_bounds(month: str):
    try:
        year, mon = map(int, month.split("-"))
        start = Date(year, mon, 1)
        end = Date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
        return start, end
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM format")


def round_half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def working_days(start: Date, end: Date) -> int:
    if end < start:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def month_log_match(month: str, emp_code=None):
    start, end = month_bounds(month)
    match = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
    if emp_code is not None:
        match["emp_code"] = emp_code
    return match


def present_day_expr():
    return {"$cond": [
        {"$and": [
            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
            {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
        ]},
        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
        0.0,
    ]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    start, end = month_bounds(month)
    effective_start = max(start, Date.fromisoformat(emp["joined_on"]))
    workdays = working_days(effective_start, end) if effective_start <= end else 0
    pipeline = [
        {"$match": month_log_match(month, emp_code)},
        {"$group": {"_id": None,
            "present_days": {"$sum": present_day_expr()},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    stats = rows[0] if rows else {}
    present = round_half_up(stats.get("present_days", 0), 2)
    pct = round_half_up(present / workdays * 100, 2) if workdays else None
    return {"emp_code": emp_code, "month": month, "working_days": workdays,
        "present_days": present, "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(present / workdays * 100, 4) if workdays else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start, end = month_bounds(month)
    emp_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", start.isoformat()]},
                {"$lte": ["$date", end.isoformat()]},
            ]}}},
            {"$group": {"_id": None,
                "present_days": {"$sum": present_day_expr()},
                "avg_work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                "avg_work_hours_n": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }},
        ], "as": "stats"}},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$stats.avg_work_hours_sum", 0]}},
            "work_hours_n": {"$sum": {"$ifNull": ["$stats.avg_work_hours_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for row in rows:
        if row["headcount"] <= 0:
            continue
        items.append({"department": row["_id"], "headcount": int(row["headcount"]),
            "present_days": round_half_up(row.get("present_days", 0), 2),
            "avg_work_hours": round_half_up(row["work_hours_sum"] / row["work_hours_n"], 2) if row.get("work_hours_n", 0) else None,
            "late_count": int(row.get("late_count", 0)), "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)), "on_duty_count": int(row.get("on_duty_count", 0))})
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1, "rank": 1}},
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: Date = Query(..., alias="from"), to_date: Date = Query(..., alias="to")):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")
    start_dt = datetime.combine(from_date, time.min, tzinfo=UTC)
    end_dt = datetime.combine(to_date, time.min, tzinfo=UTC)
    pipeline = [
        {"$match": {"date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"_id": {"$literal": from_date.isoformat()}, "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}},
        ]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"daystr": "$date"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$daystr"]}]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},
        {"$sort": {"date": 1}},
    ]
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    # Densify can produce a row for each day; normalize numeric output and nulls.
    for item in items:
        item["present_count"] = round_half_up(item.get("present_count", 0), 2)
        item["attendance_rate"] = round_half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = round_half_up(item.get("moving_avg_7d"), 4)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[Date] = None,
    date_to: Optional[Date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[Date] = Query(None, alias="from"),
    to_date: Optional[Date] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        query = {}
        if emp_code is not None: query["emp_code"] = emp_code
        if status is not None: query["status"] = status
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None: query["date"]["$gte"] = date_from.isoformat()
            if date_to is not None: query["date"]["$lte"] = date_to.isoformat()
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        cmd = {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(status_code=422, detail="emp_code and month are required")
        start, end = month_bounds(month)
        pipeline = [{"$match": month_log_match(month, emp_code)}, {"$group": {"_id": "$emp_code", "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}}]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(status_code=422, detail="month is required")
        start, end = month_bounds(month)
        match = {"joined_on": {"$lte": end.isoformat()}}
        if department is not None: match["department"] = department
        pipeline = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}, {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}}, {"$match": {"$or": [{"logs": None}, {"logs.date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}]}}, {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}]
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(status_code=422, detail="month is required")
        start, end = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}, {"$sort": {"total_late_minutes": -1}}]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        collection = "attendance_logs"
    else:
        if not department or from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid trend date range")
        pipeline = [{"$match": {"date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}}, {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}}, {"$match": {"employee.department": department}}]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        collection = "attendance_logs"
    try:
        explain = db.command({"explain": cmd, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"explain failed: {exc.__class__.__name__}")
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
