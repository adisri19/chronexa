import os
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app, db

TEST_DB_NAME = "chronexa_test"

@pytest.fixture(scope="session")
def test_client():
    client = TestClient(app)
    yield client

@pytest.fixture(autouse=True)
def clean_test_db():
    # Use isolated test database
    orig_db_name = db.name
    test_db = db.client[TEST_DB_NAME]
    
    # Point collections to test database
    orig_employees = db.employees
    orig_attendance = db.attendance_logs
    
    db.employees = test_db.employees
    db.attendance_logs = test_db.attendance_logs
    
    # Ensure indexes on test db
    test_db.employees.create_index([("emp_code", 1)], unique=True)
    test_db.employees.create_index([("department", 1), ("emp_code", 1)])
    test_db.employees.create_index([("department", 1), ("joined_on", 1)])
    test_db.employees.create_index([("joined_on", 1)])
    
    test_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    test_db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    test_db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    test_db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    
    # Clear collections before each test
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})
    
    yield
    
    # Teardown
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})
    db.employees = orig_employees
    db.attendance_logs = orig_attendance
