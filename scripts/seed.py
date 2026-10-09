"""
Chronexa Database Seeder
Seeds sample employees and realistic attendance records into MongoDB.
"""

import os
import random
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

DEPARTMENTS = ["Engineering", "Product", "Design", "Marketing", "Finance", "HR", "Operations", "QA"]

def seed_sample_data(num_employees: int = 10, num_days: int = 14):
    client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc)
    db = client[MONGO_DB]
    
    print(f"Connecting to MongoDB: {MONGO_URI}/{MONGO_DB}")
    print(f"Seeding {num_employees} employees and up to {num_days} days of attendance records...")
    
    # Clean previous demo data with prefix EMP_DEMO
    db.employees.delete_many({"emp_code": {"$regex": r"^EMP9\d{3}$"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP9\d{3}$"}})
    
    employees = []
    base_date = datetime(2026, 7, 1, tzinfo=timezone.utc)
    
    for i in range(1, num_employees + 1):
        emp_code = f"EMP9{i:03d}"
        dept = DEPARTMENTS[i % len(DEPARTMENTS)]
        emp = {
            "emp_code": emp_code,
            "name": f"Demo Employee {i}",
            "email": f"demo.emp{i}@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-06-01",
            "created_at": datetime(2026, 6, 1, 4, 0, 0, tzinfo=timezone.utc),
        }
        employees.append(emp)
        
    db.employees.insert_many(employees)
    print(f"Successfully seeded {len(employees)} employees.")
    
    logs = []
    for emp in employees:
        emp_code = emp["emp_code"]
        for d in range(num_days):
            cur_date = base_date + timedelta(days=d)
            date_str = cur_date.strftime("%Y-%m-%d")
            
            # Weekend logic
            if cur_date.weekday() >= 5:
                continue
                
            # Random status
            r = random.random()
            if r < 0.05:
                status = "LEAVE"
                logs.append({
                    "emp_code": emp_code,
                    "date": date_str,
                    "status": status,
                    "punch_in": None,
                    "punch_out": None,
                    "work_hours": None,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                })
            elif r < 0.10:
                status = "ABSENT"
                logs.append({
                    "emp_code": emp_code,
                    "date": date_str,
                    "status": status,
                    "punch_in": None,
                    "punch_out": None,
                    "work_hours": None,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                })
            else:
                status = "PRESENT"
                # 09:30 IST is 04:00 UTC
                late_min = random.choice([0, 0, 0, 10, 15, 25])
                punch_in_utc = datetime(cur_date.year, cur_date.month, cur_date.day, 4, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=late_min)
                
                # Punch out 9 hours later + optional overtime
                ot_min = random.choice([0, 0, 30, 45])
                punch_out_utc = punch_in_utc + timedelta(hours=9, minutes=ot_min)
                
                diff_sec = int((punch_out_utc - punch_in_utc).total_seconds())
                work_hours = round(diff_sec / 3600.0, 2)
                
                logs.append({
                    "emp_code": emp_code,
                    "date": date_str,
                    "status": status,
                    "punch_in": punch_in_utc,
                    "punch_out": punch_out_utc,
                    "work_hours": work_hours,
                    "late_minutes": late_min,
                    "overtime_minutes": ot_min,
                    "half_day": False,
                    "history": [],
                })
                
    if logs:
        db.attendance_logs.insert_many(logs)
    print(f"Successfully seeded {len(logs)} attendance logs.")

if __name__ == "__main__":
    seed_sample_data()
