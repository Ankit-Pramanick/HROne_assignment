# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique on `emp_code` (uniqueness and concurrency safety); compound on `(department, emp_code)` (filtered and sorted listings); `(department, joined_on)` and `joined_on` (analytics headcount lookups).
   - `attendance_logs`: Unique compound on `(emp_code, date)` (guarantees one record per employee-day, punch-in race safety); `(date: -1, emp_code: 1)` (index-covered sorted pagination); `(emp_code: 1, punch_in: -1)` (fast punch-out open record lookup); `(date: 1, late_minutes: 1)` (leaderboard filtering).
   - *Rejected*: A standalone index on `status` was rejected due to low cardinality (only 5 distinct values), which yields poor index selectivity.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `db.attendance_logs.insert_one()`. MongoDB's unique compound index on `(emp_code, date)` atomically accepts the first insert and rejects the second with `DuplicateKeyError`. The winner returns 201 Created; the loser catches the error and returns 409 Conflict. No check-then-insert race condition exists.

3. **Ties.**
   The leaderboard uses MongoDB `$setWindowFields` with `$rank` for standard competition ranking (1, 2, 2, 4). The `{ rank: { $lte: limit } }` filter is applied after ranking. Both tied employees share the same rank and are returned (ordered by `total_late_minutes` descending, then `emp_code` ascending). Consequently, the response may contain more than `limit` rows.

4. **Headcount.**
   The `department_summary` pipeline starts on the `employees` collection (matching `joined_on <= month_end`), then performs a `$lookup` to join attendance records. Employees with zero logs receive an empty logs array, but are still counted toward the department's `headcount` via `{ $sum: 1 }`.

5. **100x Data.**
   I would implement pre-aggregated daily and monthly summary rollups updated via MongoDB change streams or background workers, transforming real-time full-collection scans and pipeline aggregations into instantaneous O(1) reads.
