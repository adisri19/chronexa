from datetime import datetime, timezone
from decimal import Decimal
from app.main import (
    ms_to_dt,
    dt_to_ms,
    attendance_date,
    late_minutes_for,
    overtime_for,
    work_hours_for,
    half_day_for,
    r8,
)

def test_r1_timestamp_truncation_and_timezone():
    # 1783311300999 ms -> truncated to whole seconds (1783311300000 ms)
    dt = ms_to_dt(1783311300999)
    assert dt.microsecond == 0
    assert dt_to_ms(dt) == 1783311300000
    
    # 2026-07-06 09:30:00 IST is 04:00:00 UTC
    dt_utc = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    date_str = attendance_date(dt_utc, "09:30", "18:30")
    assert date_str == "2026-07-06"

def test_r1_overnight_shift_date_attribution():
    # Overnight shift: 22:00 to 06:00
    # Punch-in at 05:00:00 IST on 2026-07-07 (23:30:00 UTC on 2026-07-06)
    # Earlier than shift_end (06:00) -> belongs to shift starting 2026-07-06
    dt_utc = datetime(2026, 7, 6, 23, 30, 0, tzinfo=timezone.utc)
    date_str = attendance_date(dt_utc, "22:00", "06:00")
    assert date_str == "2026-07-06"
    
    # Punch-in at 22:15:00 IST on 2026-07-06 (16:45:00 UTC on 2026-07-06)
    # Greater than shift_end (06:00) -> belongs to 2026-07-06
    dt_start_utc = datetime(2026, 7, 6, 16, 45, 0, tzinfo=timezone.utc)
    assert attendance_date(dt_start_utc, "22:00", "06:00") == "2026-07-06"

def test_r2_late_minutes_boundary():
    # Shift start: 09:30:00 IST
    # Exactly at grace limit (09:40:00 IST, 600 seconds) -> 0 late minutes
    dt_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert late_minutes_for(dt_grace, "2026-07-06", "09:30", "18:30") == 0
    
    # 1 second past grace (09:40:01 IST, 601 seconds) -> 10 late minutes
    dt_late_1s = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert late_minutes_for(dt_late_1s, "2026-07-06", "09:30", "18:30") == 10
    
    # 10:15:59 IST (2759 seconds) -> 45 late minutes
    dt_late_45m = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert late_minutes_for(dt_late_45m, "2026-07-06", "09:30", "18:30") == 45

def test_r3_overtime_minutes_boundary():
    # Shift end: 18:30:00 IST (13:00:00 UTC)
    # 29 minutes 59 seconds overtime (1799s) -> under 30m threshold -> 0
    dt_ot_29m = datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc)
    assert overtime_for(dt_ot_29m, "2026-07-06", "09:30", "18:30") == 0
    
    # Exactly 30 minutes overtime (1800s, 19:00:00 IST) -> 30
    dt_ot_30m = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert overtime_for(dt_ot_30m, "2026-07-06", "09:30", "18:30") == 30
    
    # 35 minutes overtime (19:05:00 IST) -> 35
    dt_ot_35m = datetime(2026, 7, 6, 13, 35, 0, tzinfo=timezone.utc)
    assert overtime_for(dt_ot_35m, "2026-07-06", "09:30", "18:30") == 35

def test_r4_work_hours_decimal_half_up():
    # 3618 seconds = 1.005 hours
    # Float round(1.005, 2) in Python produces 1.0 (banker's rounding)
    # Decimal ROUND_HALF_UP produces 1.01
    dt_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    dt_out = datetime(2026, 7, 6, 5, 0, 18, tzinfo=timezone.utc)
    assert work_hours_for(dt_in, dt_out) == 1.01

def test_r5_half_day_boundary():
    # Exactly 4.49 hours -> half_day = True
    assert half_day_for(4.49) is True
    # Exactly 4.50 hours -> half_day = False
    assert half_day_for(4.50) is False
    # None (open record) -> False
    assert half_day_for(None) is False

def test_r8_quantization():
    assert r8(95.238, 2) == 95.24
    assert r8(0.95238, 4) == 0.9524
    assert r8(None, 2) is None
