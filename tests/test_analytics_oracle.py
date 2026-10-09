from decimal import Decimal
from app.main import db

def test_zero_log_employee_headcount_in_department_summary(test_client):
    # EMP1 has 1 log, EMP2 has 0 logs
    test_client.post("/employees", json={
        "emp_code": "EMP0301",
        "name": "Active Logger",
        "email": "active@example.com",
        "department": "Analytics",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    test_client.post("/employees", json={
        "emp_code": "EMP0302",
        "name": "Zero Logger",
        "email": "zero@example.com",
        "department": "Analytics",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    # Punch in for EMP1
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0301",
        "punched_at": 1783310400000,
    })
    test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0301",
        "punched_at": 1783344000000,
    })
    
    # Department summary must report headcount = 2
    res = test_client.get("/analytics/departments/summary?month=2026-07&department=Analytics")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "Analytics"
    assert items[0]["headcount"] == 2
    assert items[0]["present_days"] == 1.0

def test_leaderboard_competition_rank_ties(test_client):
    # Two employees tied with 30 late minutes, one with 15 minutes
    for code, name, dept in [("EMP0303", "Tied A", "Sales"), ("EMP0304", "Tied B", "Sales"), ("EMP0305", "Lower C", "Sales")]:
        test_client.post("/employees", json={
            "emp_code": code,
            "name": name,
            "email": f"{code.lower()}@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        })
    # EMP0303: 30 min late
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0303", "punched_at": 1783312200000}) # 10:00 IST
    # EMP0304: 30 min late
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0304", "punched_at": 1783312200000})
    # EMP0305: 15 min late
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0305", "punched_at": 1783311300000}) # 09:45 IST

    res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    # Ranks should be 1, 1, and since both rank 1 <= limit 2, both are present
    assert len(items) >= 2
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1
    assert items[0]["total_late_minutes"] == 30
    assert items[1]["total_late_minutes"] == 30

def test_trend_continuous_calendar_gap_filling(test_client):
    test_client.post("/employees", json={
        "emp_code": "EMP0306",
        "name": "Trend Guy",
        "email": "trend@example.com",
        "department": "Operations",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    # Only punch in on 2026-07-06 (Monday). Query for 2026-07-04 (Sat) to 2026-07-07 (Tue) -> 4 days
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0306",
        "punched_at": 1783310400000,
    })
    
    res = test_client.get("/analytics/departments/Operations/trend?from=2026-07-04&to=2026-07-07")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 4
    dates = [item["date"] for item in data["items"]]
    assert dates == ["2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07"]
    # Saturday and Sunday should have present_count = 0.0
    assert data["items"][0]["is_working_day"] is False
    assert data["items"][0]["present_count"] == 0.0
    assert data["items"][2]["is_working_day"] is True
    assert data["items"][2]["present_count"] == 1.0
