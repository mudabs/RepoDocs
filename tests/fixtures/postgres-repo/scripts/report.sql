-- Reporting query only; no schema definitions here.
SELECT c.email_address, count(o.id)
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.email_address;
