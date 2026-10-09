# Chronexa Architectural Decisions

1. **Indexes.** I created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` to guarantee entity uniqueness and race-free writes. For querying, I indexed `(date, emp_code)`, `(status, date, emp_code)`, and `(emp_code, punch_in)` on logs, plus `(department, joined_on)` on employees. I considered and rejected denormalizing department into attendance logs because department belongs to the employee profile and would introduce stale data.

2. **Punch-in race.** When two identical punch-in requests arrive simultaneously, both compute shift rules and attempt `insert_one`. The unique index on `(emp_code, date)` lets one succeed (201) and throws `DuplicateKeyError` on the second, returning 409 without check-then-insert races.

3. **Ties.** For ties at the limit cutoff, I compute competition ranks using `$rank` in `$setWindowFields`. Limit filtering occurs after ranking, so all employees tied at or below the cutoff rank are returned, ordered by `emp_code`.

4. **Headcount.** Department summary roots on `employees` matching `joined_on <= month_end`, joining logs via `$lookup`. Employees with zero logs produce an empty array and are fully counted toward headcount.

5. **One thing you would change.** At 100x scale, I would maintain pre-aggregated daily departmental rollups via scheduled background `$merge` pipelines to eliminate on-the-fly log scans.
