-- fct_daily_revenue_rolling_avg.sql
--
-- Source(s): fct_daily_revenue
-- Target:    mart_revenue_trends (not materialized as its own table;
--            this reporting-layer query is consumed directly by the
--            dashboard's trend widget)
--
-- Downstream "mart" model built on top of fct_daily_revenue. Computes a
-- 7-day trailing rolling average of revenue per region using a window
-- function, smoothing out day-to-day noise for the dashboard trend line.

SELECT
    date,
    region,
    total_revenue,
    AVG(total_revenue) OVER (
        PARTITION BY region
        ORDER BY date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ) AS revenue_7d_rolling_avg
FROM fct_daily_revenue
ORDER BY region, date;
