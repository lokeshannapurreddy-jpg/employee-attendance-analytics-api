# REVIEW.md

## Findings Against the Starter and API Contract

| # | Category | Area | Finding | Correction or Status |
|---|---|---|---|---|
| 1 | Existing starter defect | Punch-in and overnight shifts | The starter assigned attendance to the punch-in's local calendar date without accounting for overnight shifts. An early-morning punch-in for a 22:00–06:00 shift can belong to the previous shift date. | Determine the applicable shift date before creating the attendance record. |
| 2 | Existing starter defect | Late-arrival calculation | The starter converted elapsed time to whole minutes before checking the 10-minute grace period. At 09:40:01 for a 09:30 shift, this could incorrectly produce 0 late minutes instead of 10. | Compare elapsed seconds strictly against 600 seconds, then calculate whole elapsed minutes using floor division. |
| 3 | Missing contract functionality | Attendance and analytics endpoints | The starter explicitly listed punch-out, attendance regularization, employee monthly analytics, department summary, late leaderboard, department trend, and explainability as TODO items. These were missing implementations rather than defects in existing endpoints. | Implement the required routes and their documented request, response, and business-rule behavior. |
| 4 | Query efficiency and calculation compliance | Department summary | The starter did not provide a department-summary implementation. The completed implementation needs to include eligible employees, including employees without attendance logs, use suitable indexes, and apply the specified half-up rounding to average work hours. | Build the aggregation from eligible employees, use an index-compatible joining-date filter, and use decimal arithmetic for half-up rounding. |

## Verification Results Observed

The following results were observed during live API checks:

- The employee-list endpoint returned HTTP 200 with employee records.
- The attendance-list endpoint returned HTTP 200 for a requested date range.
- The employee monthly analytics endpoint returned HTTP 200.
- The department summary returned HTTP 200, including when filtered to Engineering.
- The department trend returned all three dates in the requested inclusive range of 2026-10-09 through 2026-10-11.
- A punch-out request returned a saved attendance record.
- Attendance regularization returned HTTP 200, and the updated record included an audit-history entry.
- All five explain endpoint variants returned HTTP 200. Their examined query plans showed index scans, with no collection scans reported in the relevant examined query or lookup stages.
- A read-only MongoDB arithmetic test confirmed that the decimal-based half-up expression converts 1.005 to 1.01.
- The live department-summary explain output contained the decimal conversion and half-up rounding expression after deployment.

## Further Tests to Confirm Before Final Submission

The results captured during this review do not independently establish all boundary and concurrency requirements. Before claiming these tests have passed, verify:

- Concurrent punch-ins for the same employee and attendance date: exactly one request should succeed, with competing requests receiving HTTP 409.
- The overtime threshold at 18:59:59 and 19:00:00 for a shift ending at 18:30.
- Half-day classification immediately below and at the 4.50-hour threshold, using the rounded work-hour value.
- Department headcount when eligible employees have no attendance records.
- Behavior and query efficiency at the assignment's larger dataset size.

The observed query plans demonstrate index usage for the current dataset. They do not, by themselves, establish performance at approximately 100,000 records.
