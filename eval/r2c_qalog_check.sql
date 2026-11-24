-- R2c 最终复测：修复生效验收 + 应答题拒答三类归因（只读）
-- 评测窗口：qa_log id 1260..1309（redline_20261124_233521，50 题，题序 B01..T15 与 id 递增一一对应）
-- 跑法：docker compose -f infra/docker-compose.yml exec -T postgres \
--          psql -U ai_rag -d ai_rag -f /dev/stdin < eval/r2c_qalog_check.sql

\echo '=== A. rerank 修复验收：scored>0 且 degrade=null 的题数（期望 ≥48/50） ==='
SELECT count(*) AS n_total,
       count(*) FILTER (WHERE (score_snapshot->'rerank'->>'scored')::int > 0
                          AND score_snapshot->'rerank'->>'degrade' IS NULL) AS scored_pos_degrade_null,
       count(*) FILTER (WHERE (score_snapshot->'rerank'->>'scored')::int = 0) AS scored_zero,
       count(*) FILTER (WHERE score_snapshot->'rerank'->>'degrade' IS NOT NULL) AS degrade_not_null,
       count(*) FILTER (WHERE (score_snapshot->'rerank'->>'degraded')::bool) AS degraded_true,
       count(*) FILTER (WHERE score_snapshot->'rerank'->>'fallback' IS NOT NULL
                          AND score_snapshot->'rerank'->>'fallback' <> 'null') AS fallback_not_null
FROM qa_log WHERE id BETWEEN 1260 AND 1309;

\echo '=== B. 零候选应答题：after_dedup=0 的题（期望 0；T01 超范围是合理值） ==='
SELECT id, left(question,34) AS question,
       score_snapshot->>'after_dedup' AS after_dedup,
       score_snapshot->>'detail' AS detail,
       gate1_verdict
FROM qa_log
WHERE id BETWEEN 1260 AND 1309
  AND (score_snapshot->>'after_dedup')::int = 0
ORDER BY id;

\echo '=== C. 应答题逐题诊断（前 35 题 = id 1260..1294）：top1 / gate / reject_reason / after_dedup ==='
SELECT id-1259 AS qn,
       gate1_verdict AS g1, gate2_verdict AS g2, reject_reason AS rej,
       (score_snapshot->>'top1')::numeric AS top1,
       (score_snapshot->'rerank'->>'scored')::int AS scored,
       score_snapshot->'rerank'->>'degrade' AS degrade,
       (score_snapshot->>'after_dedup')::int AS dedup,
       latency_ms AS ms
FROM qa_log WHERE id BETWEEN 1260 AND 1294
ORDER BY id;

\echo '=== D. 应答题拒答三类归因汇总 ==='
WITH a AS (
  SELECT id,
    gate1_verdict AS g1, gate2_verdict AS g2, reject_reason AS rej,
    coalesce((score_snapshot->>'top1')::numeric, -1) AS top1,
    (score_snapshot->>'after_dedup')::int AS dedup
  FROM qa_log WHERE id BETWEEN 1260 AND 1294
)
SELECT count(*) AS answerable_n,
  count(*) FILTER (WHERE g1 IN ('pass') ) AS entered_gen_or_pass,
  -- 检索没召回：top1 低（<0.20）或 null(-1)
  count(*) FILTER (WHERE top1 < 0.20) AS retrieval_miss_low_top1,
  -- 门控拦：top1 高（>=0.20）但 gate1 非 pass
  count(*) FILTER (WHERE top1 >= 0.20 AND g1 <> 'pass') AS gate_blocked_high_top1,
  -- clarify（多候选消歧，top1 高但被判多义）
  count(*) FILTER (WHERE g1 = 'clarify') AS clarify,
  -- 生成层/NLI：gate1=pass 但 gate2 未 supported 或 reject
  count(*) FILTER (WHERE g1 = 'pass' AND (g2 IS DISTINCT FROM 'supported')) AS gen_layer_blocked
FROM a;
