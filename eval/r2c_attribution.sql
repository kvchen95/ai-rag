-- R2c 应答题三类归因（最终，修正 reject_reason 为顶层列）
-- 评测窗口 id 1260..1294 = 35 道应答题。
\echo '=== 按失败机制分类（35 应答题）==='
WITH a AS (
  SELECT id-1259 AS qn,
    coalesce(score_snapshot->>'detail','') AS detail,
    coalesce(score_snapshot->'verify'->'failure'->>'check','') AS vcheck,
    coalesce(reject_reason,'') AS rej,
    gate1_verdict AS g1, gate2_verdict AS g2,
    coalesce((score_snapshot->>'top1')::numeric,-1) AS top1,
    (score_snapshot->>'after_dedup')::int AS dedup
  FROM qa_log WHERE id BETWEEN 1260 AND 1294
), c AS (
  SELECT *,
    CASE
      WHEN g1='pass' AND g2='supported' THEN 'answered_ok'
      WHEN detail LIKE '%两份文档分数接近%' THEN 'gate_clarify'
      WHEN rej='low_confidence' THEN 'gate_reject_low'
      WHEN detail LIKE '%检索无候选%' OR top1 < 0.20 THEN 'retrieval_miss'
      WHEN vcheck='draft_status' THEN 'gen_draft_status_unknown'
      WHEN rej='model_timeout' THEN 'gen_model_timeout'
      WHEN vcheck='nli_support' OR rej='unverifiable' THEN 'gen_nli_not_supported'
      ELSE 'other'
    END AS cause
  FROM a
)
SELECT cause, count(*) AS n, min(top1) AS min_top1, max(top1) AS max_top1
FROM c GROUP BY cause ORDER BY n DESC;

\echo '=== 三类汇总（对照任务 step6：检索没召回 / 门控拦 / 生成NLI拦）==='
WITH a AS (
  SELECT id,
    coalesce(score_snapshot->>'detail','') AS detail,
    coalesce(score_snapshot->'verify'->'failure'->>'check','') AS vcheck,
    coalesce(reject_reason,'') AS rej,
    gate1_verdict AS g1, gate2_verdict AS g2,
    coalesce((score_snapshot->>'top1')::numeric,-1) AS top1
  FROM qa_log WHERE id BETWEEN 1260 AND 1294
)
SELECT count(*) AS answerable_total,
  count(*) FILTER (WHERE g1='pass' AND g2='supported') AS answered_ok,
  count(*) FILTER (WHERE detail LIKE '%检索无候选%' OR top1 < 0.20) AS cat1_retrieval_miss,
  count(*) FILTER (WHERE detail LIKE '%两份文档分数接近%' OR rej='low_confidence') AS cat2_gate_block,
  count(*) FILTER (WHERE vcheck='draft_status' OR rej='model_timeout' OR vcheck='nli_support' OR rej='unverifiable') AS cat3_gen_nli_block
FROM a;
