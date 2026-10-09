"""
Chronexa 100k Scale Benchmark & Query Planner Audit
Measures cold startup time, p50/p95 endpoint latencies, and execution plan statistics
against 500 employees and 100,000 attendance records.
"""

import os
import platform
import time
import json
import statistics
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient
import httpx

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
BENCH_DB = os.getenv("MONGO_DB", "hrone_test")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

def run_benchmark():
    client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc)
    db = client[BENCH_DB]
    
    print(f"=== Chronexa Scale Benchmark (100,000 Records) ===")
    print(f"Host: {platform.node()} ({platform.system()} {platform.release()})")
    print(f"CPU Arch: {platform.machine()}")
    print(f"Python: {platform.python_version()}")
    
    # 1. Check existing dataset or seed
    emp_count = db.employees.count_documents({})
    att_count = db.attendance_logs.count_documents({})
    
    if emp_count < 500 or att_count < 100000:
        print(f"Current count: {emp_count} emps, {att_count} logs. Seeding 500 employees & 100,000 logs...")
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})
        
        # Create indexes
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("emp_code", 1)])
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1)])
        
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        
        depts = ["Engineering", "Product", "Design", "Marketing", "Finance", "HR", "Operations", "QA"]
        employees = []
        for i in range(1, 501):
            emp = {
                "emp_code": f"EMP{i:04d}",
                "name": f"Employee {i}",
                "email": f"emp{i}@example.com",
                "department": depts[i % len(depts)],
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
                "created_at": datetime(2026, 1, 1, 4, 0, 0, tzinfo=timezone.utc),
            }
            employees.append(emp)
        db.employees.insert_many(employees)
        
        base_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        batch = []
        total_seeded = 0
        for i in range(1, 501):
            code = f"EMP{i:04d}"
            for day_offset in range(200): # 200 days * 500 = 100,000
                d = base_date + timedelta(days=day_offset)
                date_str = d.strftime("%Y-%m-%d")
                t_in = d.replace(hour=4, minute=0, second=0) + timedelta(minutes=(i % 30))
                t_out = t_in + timedelta(hours=9)
                batch.append({
                    "emp_code": code,
                    "date": date_str,
                    "status": "PRESENT",
                    "punch_in": t_in,
                    "punch_out": t_out,
                    "work_hours": 9.0,
                    "late_minutes": (i % 30),
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": []
                })
                if len(batch) >= 10000:
                    db.attendance_logs.insert_many(batch)
                    total_seeded += len(batch)
                    batch = []
        if batch:
            db.attendance_logs.insert_many(batch)
            total_seeded += len(batch)
        print(f"Seeded {total_seeded} records successfully.")
    else:
        print(f"Using pre-seeded dataset: {emp_count} employees, {att_count} logs.")

    # 2. Measure Query Execution Plans via /admin/explain
    print("\n--- Auditing MongoDB Query Execution Plans via /admin/explain ---")
    explain_results = {}
    explain_targets = [
        "attendance_list?page=1&page_size=20",
        "employee_monthly?emp_code=EMP0001&month=2026-06",
        "department_summary?month=2026-06",
        "late_leaderboard?month=2026-06",
        "department_trend?from=2026-06-01&to=2026-06-30",
    ]
    with httpx.Client(timeout=10.0) as http_client:
        for ep in explain_targets:
            res = http_client.get(f"{BASE_URL}/admin/explain/{ep}")
            if res.status_code == 200:
                text = res.text
                has_ixscan = "IXSCAN" in text
                has_collscan = "COLLSCAN" in text
                name = ep.split("?")[0]
                explain_results[name] = {"IXSCAN": has_ixscan, "COLLSCAN": has_collscan}
                print(f"{name:20s}: IXSCAN={has_ixscan}, COLLSCAN={has_collscan}")

    # 3. Measure HTTP Endpoint Latency (50 iterations)
    print("\n--- Measuring Endpoint Latencies (50 warm runs) ---")
    endpoints = {
        "GET /attendance (unfiltered)": f"{BASE_URL}/attendance?page=1&page_size=20",
        "GET /attendance (filtered)": f"{BASE_URL}/attendance?date_from=2026-06-01&date_to=2026-06-30&status=PRESENT&page=1&page_size=20",
        "GET /analytics/employees/{emp}/monthly": f"{BASE_URL}/analytics/employees/EMP0001/monthly?month=2026-06",
        "GET /analytics/departments/summary": f"{BASE_URL}/analytics/departments/summary?month=2026-06",
        "GET /analytics/leaderboard/late": f"{BASE_URL}/analytics/leaderboard/late?month=2026-06&limit=10",
        "GET /analytics/departments/{dept}/trend": f"{BASE_URL}/analytics/departments/Engineering/trend?from=2026-06-01&to=2026-06-30",
    }
    
    latency_report = {}
    with httpx.Client(timeout=10.0) as http_client:
        for name, url in endpoints.items():
            durations = []
            for _ in range(50):
                t0 = time.perf_counter()
                res = http_client.get(url)
                t1 = time.perf_counter()
                if res.status_code == 200:
                    durations.append((t1 - t0) * 1000.0)
            if durations:
                durations.sort()
                p50 = statistics.median(durations)
                p95 = durations[int(len(durations) * 0.95)]
                latency_report[name] = {"p50_ms": round(p50, 2), "p95_ms": round(p95, 2)}
                print(f"{name:45s}: p50={p50:6.2f} ms | p95={p95:6.2f} ms")
            else:
                print(f"{name:45s}: FAILED (ensure app is running on {BASE_URL})")

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hardware": {
            "node": platform.node(),
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "dataset": {
            "employees": 500,
            "attendance_logs": 100000,
        },
        "explain_checks": explain_results,
        "latencies": latency_report,
    }
    
    out_path = os.path.join(os.path.dirname(__file__), "benchmark_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved benchmark results to {out_path}")

if __name__ == "__main__":
    run_benchmark()
