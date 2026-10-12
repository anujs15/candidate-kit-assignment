# Design decisions

1. **Indexes.** I created unique indexes on `employees.emp_code` and `(attendance_logs.emp_code, date)`. They enforce identity and race-safe punch-in. Employee department/join-date indexes support headcount, summary, and trend queries. Attendance indexes support employee/month ranges, the list sort, and late analytics. I rejected an index on `status` alone because it has only five values and poor selectivity.

2. **Punch-in race.** Both requests may read the employee and build the same record. MongoDB accepts the first insert. The unique `(emp_code, date)` index rejects the second with `DuplicateKeyError`, which becomes HTTP 409. Exactly one returns 201.

3. **Ties.** The pipeline uses `$rank`, so equal late-minute totals share a competition rank. Filtering happens after ranking; everyone with rank at most `limit` is returned, including a tie that makes the response longer than the limit.

4. **Headcount.** Department summary starts from eligible employees and looks up their logs. `preserveNullAndEmptyArrays` keeps employees with no matching logs, and grouping distinct employee codes counts them.

5. **At 100x scale.** I would replace offset pagination with stable cursor pagination, archive old attendance by period, and review shard keys and explain statistics using production query patterns.
