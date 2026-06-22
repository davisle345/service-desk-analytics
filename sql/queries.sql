-- The same analyses as analyze_tickets.py, in plain SQL.
-- Run against the SQLite db the pipeline writes:
--   sqlite3 output/tickets.db < sql/queries.sql

-- 1. Ticket volume by month
SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS tickets
FROM tickets
GROUP BY month
ORDER BY month;

-- 2. Tickets by category (current taxonomy)
SELECT category, COUNT(*) AS tickets
FROM tickets
GROUP BY category
ORDER BY tickets DESC;

-- 2b. Tickets by request type
SELECT request_type, COUNT(*) AS tickets
FROM tickets
GROUP BY request_type
ORDER BY tickets DESC;

-- 3. Median-ish resolution by type (SQLite has no MEDIAN; use AVG + count)
SELECT request_type,
       ROUND(AVG(resolution_hours), 1) AS avg_hours,
       COUNT(*) AS closed_tickets
FROM tickets
WHERE resolution_hours IS NOT NULL
GROUP BY request_type
ORDER BY avg_hours DESC;

-- 4. Volume by priority
SELECT priority, COUNT(*) AS tickets
FROM tickets
GROUP BY priority
ORDER BY tickets DESC;

-- 5. Tickets handled per assignee (anonymized aliases)
SELECT assignee, COUNT(*) AS tickets
FROM tickets
GROUP BY assignee
ORDER BY tickets DESC;

-- 6. Still-open tickets
SELECT ticket_id, request_type, priority, created_at
FROM tickets
WHERE status != 'closed'
ORDER BY created_at;
