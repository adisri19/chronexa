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


