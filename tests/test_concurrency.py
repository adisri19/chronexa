from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from app.main import app, db

def test_concurrent_punch_in(test_client):
    # Setup employee
    test_client.post("/employees", json={
        "emp_code": "EMP0201",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    
    # Fire 25 simultaneous punch-in calls
    def do_punch():
        c = TestClient(app)
        return c.post("/attendance/punch-in", json={
            "emp_code": "EMP0201",
            "punched_at": 1783310400000,
        }).status_code
        
    with ThreadPoolExecutor(max_workers=25) as ex:
        statuses = list(ex.map(lambda _: do_punch(), range(25)))
        
    assert statuses.count(201) == 1
    assert statuses.count(409) == 24

def test_concurrent_punch_out(test_client):
    test_client.post("/employees", json={
        "emp_code": "EMP0202",
        "name": "Out Race Tester",
        "email": "outrace@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0202",
        "punched_at": 1783310400000,
    })
    
    # Fire 25 simultaneous punch-out calls
    def do_punch_out():
        c = TestClient(app)
        return c.post("/attendance/punch-out", json={
            "emp_code": "EMP0202",
            "punched_at": 1783344000000,
        }).status_code
        
    with ThreadPoolExecutor(max_workers=25) as ex:
        statuses = list(ex.map(lambda _: do_punch_out(), range(25)))
        
    assert statuses.count(200) == 1
    assert statuses.count(409) == 24

def test_concurrent_employee_creation(test_client):
    payload = {
        "emp_code": "EMP0203",
        "name": "Emp Race Tester",
        "email": "emprace@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    def do_create():
        c = TestClient(app)
        return c.post("/employees", json=payload).status_code

    with ThreadPoolExecutor(max_workers=25) as ex:
        statuses = list(ex.map(lambda _: do_create(), range(25)))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 24

def test_concurrent_regularization(test_client):
    test_client.post("/employees", json={
        "emp_code": "EMP0204",
        "name": "Reg Race Tester",
        "email": "regrace@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0204",
        "punched_at": 1783310400000,
    })
    test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0204",
        "punched_at": 1783344000000,
    })
    
    def do_reg(idx):
        c = TestClient(app)
        return c.patch("/attendance/EMP0204/2026-07-06", json={
            "punch_in": 1783310400000 + ((idx + 1) * 60 * 1000),
            "reason": f"Concurrent edit {idx}",
            "regularized_by": f"MGR_{idx}",
        }).status_code

    with ThreadPoolExecutor(max_workers=25) as ex:
        statuses = list(ex.map(do_reg, range(25)))

    # All responses must be either 200 or 409
    for code in statuses:
        assert code in (200, 409)

    c200 = statuses.count(200)
    assert c200 >= 1
    
    # Crucial guarantee: length of history in DB must exactly equal number of successful 200 responses!
    doc = db.attendance_logs.find_one({"emp_code": "EMP0204", "date": "2026-07-06"})
    assert len(doc.get("history", [])) == c200
