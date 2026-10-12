# Starter code review

| # | Where | What was wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | Mongo client | PyMongo returned naive datetimes, making UTC/IST handling ambiguous. | Compare a seeded BSON date with an aware request timestamp. | Enabled UTC-aware BSON datetimes. |
| 2 | Startup | Required indexes were never created. | Concurrent duplicate writes succeed and explain shows `COLLSCAN`. | Added idempotent unique and query indexes in application lifespan. |
| 3 | `health` | It returned 200 without checking MongoDB. | Stop MongoDB; `/health` still says `ok`. | Ping MongoDB and return 503 on failure. |
| 4 | `compute_late_minutes` | It ignored IST, overnight dates, second truncation, and the strict grace boundary. | A 09:40:01 IST punch incorrectly produces 0. | Build the shift instant from the attendance date in IST and compare whole seconds. |
| 5 | `compute_work_hours` | Python `round` is half-even and float-based, while R4 requires decimal half-up. | A value exactly on a rounding tie rounds the wrong way. | Used `Decimal.quantize(..., ROUND_HALF_UP)`. |
| 6 | `compute_overtime` | It could attach the wrong timezone and never moved an overnight shift end to the next day. | A 22:00-06:00 shift calculates overtime against the prior morning. | Build the correct IST shift-end instant and apply the 30-minute threshold. |
| 7 | Request models | Timestamps, formats, enum values, lengths, and equal shift times were insufficiently validated. | Seconds-looking timestamps or `ABSENT` punch-ins reach endpoint code. | Added strict Pydantic constraints and a shift-time validator. |
| 8 | `create_employee` | Duplicate detection used check-then-insert and was race-prone. | Parallel creates can both pass the pre-check. | Rely on the unique index and translate `DuplicateKeyError` to 409. |
| 9 | `create_employee` | `created_at` was naive and BSON dates were returned directly instead of epoch milliseconds. | The response violates the OpenAPI schema. | Store UTC and serialize every instant at the API boundary. |
| 10 | `list_employees` | Skip used `page * page_size`, so page 1 skipped the first page. | Request page 1 from a non-empty collection. | Use `(page - 1) * page_size`. |
| 11 | `list_employees` | Total ignored the department filter and results were not sorted. | Filter by department or insert out-of-order codes. | Count the filter and sort by `emp_code`. |
| 12 | `punch_in` | It dereferenced a missing employee and returned 500. | Punch in with an unknown code. | Return 404 before computing the record. |
| 13 | `punch_in` | Local server time and naive `fromtimestamp` produced the wrong IST attendance date. | Run outside IST or use an overnight shift. | Convert epoch milliseconds to UTC and derive the date in IST per R1. |
| 14 | `punch_in` | Check-then-insert was not concurrency-safe. | Send two identical requests concurrently. | Enforce the natural key with a unique index and catch the duplicate. |
| 15 | `list_attendance` | It loaded and sorted the entire result set in Python. | Memory and latency grow sharply around 100k rows. | Filter, sort, skip, limit, and count in MongoDB. |
| 16 | `list_attendance` | Sorting lacked the `emp_code` tiebreaker and date-range order was unchecked. | Multiple employees on one date or `date_from > date_to`. | Added database sort and 422 range validation. |
| 17 | Responses | Attendance exposed a generated `id`, although the contract says records have no API id. | Inspect punch-in or list output. | Removed `_id` and return the natural-key fields only. |

I also checked the starter's positive-lateness convention: returning the full minutes since shift start, rather than minutes beyond the grace period, is correct. Keeping `load_dotenv()` is also appropriate because existing environment variables take precedence by default.
