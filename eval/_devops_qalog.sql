\echo '=== FINAL 3 ROUNDS: qa_log windows 1410-1559 ==='
SELECT count(*) AS n_calls,
       count(*) FILTER (WHERE reject_reason='model_timeout') AS model_timeout,
       count(*) FILTER (WHERE reject_reason='no_evidence') AS no_evidence,
       count(*) FILTER (WHERE reject_reason='clarify') AS clarify
FROM qa_log WHERE id BETWEEN 1410 AND 1559;
\echo '=== per-round model_timeout ==='
SELECT (id-1410)/50*50+1410 AS win_start,
       count(*) FILTER (WHERE reject_reason='model_timeout') AS mt
FROM qa_log WHERE id BETWEEN 1410 AND 1559 GROUP BY 1 ORDER BY 1;
\echo '=== any SSL EOF detail left? ==='
SELECT count(*) AS ssl_eof FROM qa_log WHERE id BETWEEN 1410 AND 1559
  AND score_snapshot->>'detail' ILIKE '%UNEXPECTED_EOF%';
