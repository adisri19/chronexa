def test_invalid_date_formats(test_client):
    # Feb 30 invalid calendar date
    res = test_client.post("/employees", json={
        "emp_code": "EMP0401",
        "name": "Bad Calendar",
        "email": "bad@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-30",
    })
    assert res.status_code == 422

def test_equal_shift_start_and_end(test_client):
    res = test_client.post("/employees", json={
        "emp_code": "EMP0402",
        "name": "Equal Shift",
        "email": "eq@example.com",
        "department": "QA",
        "shift_start": "10:00",
        "shift_end": "10:00",
        "joined_on": "2026-07-01",
    })
    assert res.status_code == 422

def test_punch_out_earlier_than_punch_in_gives_422(test_client):
    test_client.post("/employees", json={
        "emp_code": "EMP0403",
        "name": "Earlier Tester",
        "email": "earlier@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    in_ms = 1783310400000
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0403", "punched_at": in_ms})
    
    # Send punch-out instant earlier than punch-in
    res = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0403",
        "punched_at": in_ms - 1000,
    })
    assert res.status_code == 422

def test_punch_out_span_over_24h_gives_422(test_client):
    test_client.post("/employees", json={
        "emp_code": "EMP0404",
        "name": "Span Tester",
        "email": "span@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    in_ms = 1783310400000
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0404", "punched_at": in_ms})
    
    # 25 hours later -> 422
    res = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0404",
        "punched_at": in_ms + (25 * 3600 * 1000),
    })
    assert res.status_code == 422

def test_trend_range_exceeds_92_days_gives_422(test_client):
    test_client.post("/employees", json={
        "emp_code": "EMP0405",
        "name": "Range Tester",
        "email": "range@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    # Range of 120 days -> 422
    res = test_client.get("/analytics/departments/QA/trend?from=2026-01-01&to=2026-05-01")
    assert res.status_code == 422
