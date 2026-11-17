"""
SQL queries for the tracking reports

Every SQL query specific to the tracking reports. The queries are built for the
delivery_events and deliveries tables.
"""

# Base "FROM ... JOIN ... WHERE ..." per domain + period
DELIVERY_EVENTS_BASE_FROM = """
  FROM delivery_events de
  JOIN deliveries d
    ON de.message_id = d.message_id
   AND de.rcpt       = d.rcpt
  WHERE d.domain_name = %s
    AND DATE(de.ts) BETWEEN %s AND %s
"""

# 1) Counts per status
DELIVERY_COUNTS_SELECT = (
    """
SELECT
  COUNT(*)                                                     AS total_sent,
  SUM(CASE WHEN de.status = 'delivered' THEN 1 ELSE 0 END)     AS total_delivered,
  SUM(CASE WHEN de.status = 'bounced'  THEN 1 ELSE 0 END)      AS total_bounced,
  SUM(CASE WHEN de.status = 'deferred' THEN 1 ELSE 0 END)      AS total_deferred,
  SUM(CASE WHEN de.status = 'failed'   THEN 1 ELSE 0 END)      AS total_failed
"""
    + DELIVERY_EVENTS_BASE_FROM
)

# 2) Average time to the first delivered event (in minutes)
AVG_DELIVERY_TIME_SELECT_BASE = """
SELECT
  AVG(TIMESTAMPDIFF(SECOND, t.first_seen, t.first_delivered_ts) / 60.0) AS avg_delivery_time_minutes
FROM (
  SELECT
    d.message_id,
    d.rcpt,
    d.first_seen,
    MIN(CASE WHEN de.status = 'delivered' THEN de.ts END) AS first_delivered_ts
  FROM deliveries d
  LEFT JOIN delivery_events de
    ON de.message_id = d.message_id
   AND de.rcpt       = d.rcpt
   AND DATE(de.ts)   BETWEEN %s AND %s      -- (1) period applied to the events
  WHERE d.domain_name = %s                    -- (2) domain
  {maybe_email_clause}                      -- (3) optional email
  GROUP BY d.message_id, d.rcpt, d.first_seen
) t
WHERE t.first_delivered_ts IS NOT NULL
"""

# 3) Bounce breakdown
BOUNCE_BREAKDOWN_SELECT = (
    """
SELECT
  SUM(CASE WHEN de.status = 'bounced' AND de.dsn LIKE '5.%%'   THEN 1 ELSE 0 END) AS hard_bounces,
  SUM(CASE WHEN de.status = 'bounced' AND de.dsn LIKE '5.7.%%' THEN 1 ELSE 0 END) AS policy_bounces,
  SUM(CASE WHEN de.status = 'bounced' AND de.dsn LIKE '4.%%'   THEN 1 ELSE 0 END) AS soft_bounces,
  SUM(CASE WHEN de.status = 'bounced' AND de.dsn NOT LIKE '4.%%' AND de.dsn NOT LIKE '5.%%'
           THEN 1 ELSE 0 END)                                                     AS other_bounces
"""
    + DELIVERY_EVENTS_BASE_FROM
)

# 4) Performance per ISP (recipient domain)
ISP_PERFORMANCE_SELECT = (
    """
SELECT
  SUBSTRING_INDEX(d.rcpt, '@', -1)                                AS isp,
  COUNT(*)                                                         AS sent,
  SUM(CASE WHEN de.status = 'delivered' THEN 1 ELSE 0 END)         AS delivered,
  SUM(CASE WHEN de.status = 'bounced'  THEN 1 ELSE 0 END)          AS bounced,
  SUM(CASE WHEN de.status = 'deferred' THEN 1 ELSE 0 END)          AS deferred,
  SUM(CASE WHEN de.status = 'failed'   THEN 1 ELSE 0 END)          AS failed
"""
    + DELIVERY_EVENTS_BASE_FROM
)

# 5) Daily time series (date as a string, to match on the Python side)
TIMESERIES_SELECT = (
    """
SELECT
  DATE_FORMAT(de.ts, '%Y-%m-%d')                                    AS date,
  COUNT(*)                                                          AS sent,
  SUM(CASE WHEN de.status = 'delivered' THEN 1 ELSE 0 END)          AS delivered,
  SUM(CASE WHEN de.status = 'bounced'  THEN 1 ELSE 0 END)           AS bounced,
  SUM(CASE WHEN de.status = 'deferred' THEN 1 ELSE 0 END)           AS deferred,
  SUM(CASE WHEN de.status = 'failed'   THEN 1 ELSE 0 END)           AS failed
"""
    + DELIVERY_EVENTS_BASE_FROM
)
