def test_health_check_ok(test_client):
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_employee_crud_contract(test_client):
    payload = {
        "emp_code": "EMP0101",
        "name": "Maya Patel",
        "email": "maya.patel@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    # Create 201
    res = test_client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0101"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)
    
    # Duplicate 409
    res_dup = test_client.post("/employees", json=payload)
    assert res_dup.status_code == 409
    assert res_dup.json()["detail"] == "emp_code already exists"
    
    # Validation error 422
    bad_payload = dict(payload, emp_code="INVALID")
    res_bad = test_client.post("/employees", json=bad_payload)
    assert res_bad.status_code == 422
    
    # List employees 200
    res_list = test_client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    page_data = res_list.json()
    assert page_data["total"] == 1
    assert page_data["page"] == 1
    assert page_data["page_size"] == 10
    assert page_data["items"][0]["emp_code"] == "EMP0101"

def test_punch_lifecycle_contract(test_client):
    # Setup employee
    test_client.post("/employees", json={
        "emp_code": "EMP0102",
        "name": "Rohan Gupta",
        "email": "rohan.gupta@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    
    # 2026-07-06 09:45:00 IST (04:15:00 UTC) = 1783311300000 ms (15 min late)
    in_ms = 1783311300000
    res_in = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0102",
        "punched_at": in_ms,
        "status": "PRESENT",
    })
    assert res_in.status_code == 201
    data_in = res_in.json()
    assert data_in["emp_code"] == "EMP0102"
    assert data_in["date"] == "2026-07-06"
    assert data_in["punch_in"] == in_ms
    assert data_in["punch_out"] is None
    assert data_in["work_hours"] is None
    assert data_in["late_minutes"] == 15
    assert data_in["overtime_minutes"] == 0
    assert data_in["half_day"] is False
    assert data_in["history"] == []
    
    # Duplicate punch-in on same day -> 409
    res_in_dup = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0102",
        "punched_at": in_ms,
    })
    assert res_in_dup.status_code == 409
    assert res_in_dup.json()["detail"] == "already punched in for this date"
    
    # Punch-out at 18:50:00 IST (13:20:00 UTC) = 1783344000000 ms (9.08 work hours, 0 overtime because < 30m)
    out_ms = 1783344000000
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0102",
        "punched_at": out_ms,
    })
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["punch_out"] == out_ms
    assert data_out["work_hours"] == 9.08
    assert data_out["late_minutes"] == 15
    assert data_out["overtime_minutes"] == 0
    assert data_out["half_day"] is False

    # Second punch-out on already closed record -> 409
    res_out_dup = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0102",
        "punched_at": out_ms + 1000,
    })
    assert res_out_dup.status_code == 409
    assert res_out_dup.json()["detail"] == "record is already punched out"

def test_attendance_list_contract(test_client):
    test_client.post("/employees", json={
        "emp_code": "EMP0103",
        "name": "Kavita Rao",
        "email": "kavita.rao@example.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0103",
        "punched_at": 1783310400000,
    })
    
    # Valid list query
    res = test_client.get("/attendance?emp_code=EMP0103&date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    
    # date_from > date_to -> 422
    res_bad = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res_bad.status_code == 422

def test_regularize_contract(test_client):
    test_client.post("/employees", json={
        "emp_code": "EMP0104",
        "name": "Ananya Sen",
        "email": "ananya.sen@example.com",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    in_ms = 1783310400000
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0104",
        "punched_at": in_ms,
    })
    
    # Valid regularization
    out_ms = in_ms + (5 * 3600 * 1000)
    res = test_client.patch("/attendance/EMP0104/2026-07-06", json={
        "punch_out": out_ms,
        "reason": "Manager approved punch-out regularisation",
        "regularized_by": "MGR001",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 5.0
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "MGR001"
    assert h["reason"] == "Manager approved punch-out regularisation"
    assert "punch_out" in h["changes"]
    assert h["changes"]["punch_out"]["to"] == out_ms
    assert isinstance(h["at"], int)
    
    # No-op regularization -> 422
    res_noop = test_client.patch("/attendance/EMP0104/2026-07-06", json={
        "reason": "No-op regularisation",
        "regularized_by": "MGR001",
    })
    assert res_noop.status_code == 422
