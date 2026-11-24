-- R2b 修复后 7 道应答题的真实检索候选数（只读）。
-- 跑法：docker compose -f infra/docker-compose.yml exec -T postgres \
--          psql -U ai_rag -d ai_rag -f /dev/stdin < eval/qalog_zero_check.sql
\echo '=== 修复后最新评测窗口：7 道应答题的 after_dedup / detail / retrieved_ids ==='
SELECT q.id,
       left(q.question, 34) AS question,
       q.score_snapshot->>'after_dedup' AS after_dedup,
       q.score_snapshot->>'detail' AS detail,
       coalesce(jsonb_array_length(q.score_snapshot->'fused'), -1) AS fused_n,
       cardinality(q.retrieved_ids) AS retrieved_n,
       q.score_snapshot->>'top1' AS top1
FROM qa_log q
WHERE q.question IN (
  '中原证券给出的中芯国际2025年归母净利润预测是多少？',
  '东方证券对中芯国际2025-2027年归母净利润的预测分别是多少？',
  '东方证券预测的中芯国际2025年营业收入是多少（百万元）？',
  '中原证券预测的中芯国际2025年营业收入是多少（百万元）？',
  '光大证券预测的中芯国际2025年营业收入是多少（百万美元）？',
  '国信证券预测的中芯国际2025年营业收入是多少（百万美元）？',
  '多家券商对中芯国际2025年营收的预测分别是多少？口径是否一致？'
)
ORDER BY q.id DESC;

\echo '=== 对照：修复前同一批题（旧窗口 id 705-754）的 after_dedup ==='
SELECT q.id, left(q.question, 30) AS question,
       q.score_snapshot->>'after_dedup' AS after_dedup,
       q.score_snapshot->>'detail' AS detail
FROM qa_log q
WHERE q.id BETWEEN 705 AND 754
  AND q.score_snapshot->>'detail' = '超出库范围，未检索'
ORDER BY q.id;
