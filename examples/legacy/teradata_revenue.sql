/* Nightly regional revenue job (invented example for Redcliff, not from any real system).
   Teradata BTEQ: dot-commands, SEL, volatile tables, QUALIFY. */

.LOGON tdprod/etl_batch,${TD_PASSWORD};
.SET ERROROUT STDOUT;

CREATE VOLATILE TABLE shipped_orders AS (
  SEL o.order_id, o.customer_id, o.order_date, o.amount
  FROM orders o
  WHERE o.status = 'shipped'
) WITH DATA PRIMARY INDEX (order_id) ON COMMIT PRESERVE ROWS;

.IF ERRORCODE <> 0 THEN .QUIT 8;

CREATE VOLATILE TABLE customer_totals AS (
  SEL s.customer_id,
      SUM(s.amount) AS total_spent,
      COUNT(*) AS order_count,
      MAX(s.order_date) AS last_order
  FROM shipped_orders s
  GROUP BY 1
) WITH DATA PRIMARY INDEX (customer_id) ON COMMIT PRESERVE ROWS;

CREATE TABLE regional_revenue AS (
  SEL c.region,
      SUM(t.total_spent) AS revenue,
      SUM(t.order_count) AS orders,
      CASE WHEN SUM(t.total_spent) >= 300 THEN 'gold' ELSE 'standard' END AS tier
  FROM customer_totals t
  JOIN customers c ON t.customer_id = c.id
  GROUP BY c.region
) WITH DATA;

CREATE TABLE top_customer_per_region AS (
  SEL c.region, c.first_name, c.last_name, t.total_spent
  FROM customer_totals t
  JOIN customers c ON t.customer_id = c.id
  QUALIFY ROW_NUMBER() OVER (PARTITION BY c.region ORDER BY t.total_spent DESC) = 1
) WITH DATA;

.IF ERRORCODE <> 0 THEN .QUIT 8;
.LOGOFF;
.QUIT 0;
