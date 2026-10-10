# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` to prevent duplicate employee codes and a unique compound index on `attendance_logs (emp_code, date)` to enforce one attendance record per employee per day. Date/employee indexes support attendance queries, while the date/late-minutes index supports the leaderboard. The employee department/joining-date index supports department analytics. I avoided unnecessary indexes that duplicate existing index coverage.

2. **Punch-in race.** Two identical requests may both check for an existing record. The unique `(emp_code, date)` index permits only one insert. The losing request receives a duplicate-key error, which the API returns as HTTP 409 Conflict.

3. **Ties.** The leaderboard ranks employees by accumulated late minutes. Employees tied at the cutoff may all be returned, so the result can exceed `limit` rather than arbitrarily excluding a tie.

4. **Headcount.** Department summary starts from eligible employees and looks up attendance records. Employees without logs therefore remain included in headcount.

5. **100x data.** I would maintain daily analytics summaries to reduce repeated aggregation over large attendance histories, then load-test and inspect query plans before scaling further.
