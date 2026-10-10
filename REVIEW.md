# REVIEW.md

## Defects identified

| # | Where (function / area) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Punch-in endpoint and overnight shift-date calculation | A punch-in before the end of an overnight shift can be assigned to the wrong calendar date. | Test a 22:00–06:00 shift with a punch-in at 05:50. The attendance date should be the previous date. | Determine the applicable shift date before creating the attendance record. |
| 2 | Department trend endpoint | The API must accept the contract's `from` and `to` query parameter names and apply the requested inclusive date range. | Request a trend using `?from=2026-10-09&to=2026-10-11` and verify that all three dates are returned. | Map the external query parameter names to the internal date parameters and validate the range. |
| 3 | Department summary aggregation | Filtering only on a calculated joining-date field can prevent MongoDB from using an index efficiently on a large employee collection. | Inspect the winning query plan and check for an indexed scan rather than a collection scan. | Add an index-compatible joining-date filter early in the aggregation and maintain the appropriate index. |

## Other behaviors reviewed

- **Concurrent punch-ins:** A unique compound index on employee code and attendance date prevents duplicate daily records. Concurrent requests were tested; one succeeded while competing requests received HTTP 409.
- **Overtime boundary:** The 30-minute overtime threshold was tested at 18:59:59 and 19:00:00 for the configured shift.
- **Half-day boundary:** Work-hour rounding and the 4.5-hour half-day threshold were tested.
- **Employees without attendance logs:** They must remain included in eligible department headcount.
- **Department trend gaps:** The endpoint was tested over a three-day range to verify that every date is returned, including weekend dates.
- **Explain endpoints:** All five tested endpoints returned HTTP 200, and their winning query plans contained IXSCAN without COLLSCAN.

Only describe an item as a defect in the starter if the defect was present in the original starter implementation. Remove or revise any entry that does not meet that condition.
