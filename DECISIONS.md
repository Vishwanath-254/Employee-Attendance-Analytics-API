# DECISIONS.md

1. **Indexes.**
   - `employees`: `emp_code` (unique) enforces business key uniqueness. `(department, joined_on, emp_code)` covers department filtering, headcount checks, and sorted listings.
   - `attendance_logs`: `(emp_code, date)` (unique) enforces natural key uniqueness and prevents duplicate punch-ins. `(date, emp_code)` backs list attendance queries sorted by date desc, emp_code asc. `(emp_code, punch_in)` optimizes finding open punch-ins. `(date, late_minutes)` powers leaderboard aggregations.
   - *Rejected*: An index on `(emp_code, status)` was rejected due to low status cardinality (5 values) since queries always filter by date ranges or emp_code.

2. **Punch-in race.**
   Two concurrent punch-in requests for the same employee and date both construct an attendance log document. MongoDB's unique index on `(emp_code, date)` atomically accepts the first write and throws `DuplicateKeyError` for the second. The winner receives `201 Created`; the loser catches `DuplicateKeyError` and returns `409 Conflict`.

3. **Ties.**
   The leaderboard ranks employees using MongoDB's `$setWindowFields` with `rank: {}`. Tied employees share the same rank number (e.g., 1, 2, 2, 4). Filtering `rank <= limit` occurs after ranking. If employees tie at the cutoff rank, all tied employees are returned (exceeding `limit` in array size), ordered by `total_late_minutes` descending then `emp_code` ascending.

4. **Headcount.**
   The department summary pipeline starts on the `employees` collection (matching `joined_on <= last_day_of_month`), grouping by department to establish `headcount` before joining `attendance_logs` via `$lookup`. Employees with zero logs still count toward `headcount` because lookup joins logs into an array per department without dropping zero-log employees.

5. **One thing you would change for 100x data.**
   At 100x scale (~10M records), cross-collection `$lookup` joins on raw logs become an I/O bottleneck. I would denormalize `department` directly into `attendance_logs`, shard the collection on `(department, date, emp_code)`, and compute monthly summaries incrementally into materialised view collections using `$merge`.
