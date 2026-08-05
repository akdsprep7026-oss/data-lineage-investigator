-- fct_daily_revenue.sql
--
-- Source(s): stg_orders_cleaned
-- Target:    fct_daily_revenue
--
-- Aggregates cleaned orders into the daily revenue-by-region fact table
-- that powers the "Total Revenue by Region" dashboard metric. Only
-- 'completed' orders count toward recognized revenue; 'pending' and
-- 'refunded' orders are excluded on purpose.

SELECT
    DATE(created_at) AS date,
    region,
    SUM(amount) AS total_revenue
FROM stg_orders_cleaned
WHERE status = 'completed'
GROUP BY DATE(created_at), region
ORDER BY date, region;
