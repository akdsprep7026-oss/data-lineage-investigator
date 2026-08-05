-- stg_orders_cleaned.sql
--
-- Source(s): raw_orders, raw_customers
-- Target:    stg_orders_cleaned
--
-- Cleans raw_orders by:
--   1. Joining in each customer's region from raw_customers. A LEFT JOIN
--      is used (not INNER) so that orders from brand-new customers whose
--      record hasn't yet landed in raw_customers are still kept, with
--      region falling back to 'UNKNOWN', instead of being silently
--      dropped and undercounting revenue.
--   2. Filtering out cancelled orders and invalid (non-positive) amounts.
--   3. De-duplicating late-arriving duplicate raw events: raw_orders is an
--      append-only landing table, so the same order_id can show up more
--      than once (e.g. a status update re-emitted by the source system).
--      We keep only the most recently seen row per order_id using a
--      window function.

WITH ranked_orders AS (
    SELECT
        o.order_id,
        o.customer_id,
        COALESCE(c.region, 'UNKNOWN') AS region,
        o.amount,
        o.status,
        o.created_at,
        ROW_NUMBER() OVER (
            PARTITION BY o.order_id
            ORDER BY o.created_at DESC
        ) AS row_num
    FROM raw_orders AS o
    LEFT JOIN raw_customers AS c
        ON o.customer_id = c.customer_id
    WHERE o.status != 'cancelled'
      AND o.amount > 0
)
SELECT
    order_id,
    customer_id,
    region,
    amount,
    status,
    created_at
FROM ranked_orders
WHERE row_num = 1;
