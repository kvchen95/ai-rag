-- 语料年份覆盖与 effective_date 可用性核查（只读）。
-- 跑法：docker compose -f infra/docker-compose.yml exec -T postgres \
--          psql -U ai_rag -d ai_rag -f /app/eval/corpus_year_coverage.sql
-- 说明：/app 是仓库 bind-mount；本文件在宿主机 eval/ 下，容器内即 /app/eval/。
\echo '=== A. active 语料按 effective_year 分布（应覆盖 2023/2024/2025） ==='
SELECT effective_year, date_confidence, count(*) AS chunks
FROM chunks
WHERE status = 'active'
GROUP BY effective_year, date_confidence
ORDER BY effective_year NULLS LAST, date_confidence;

\echo '=== B. 7 道题 expected_doc_key 在 active 版本里的切片数 + 时间覆盖 ==='
WITH want(doc_key) AS (
  VALUES
    ('pdf_reports/zhong-yuan-zheng-quan-chan-neng-li-yong-lv-xian-zhu-ti'),
    ('pdf_reports/dong-fang-zheng-quan-chan-neng-li-yong-lv-ti-sheng-chi'),
    ('pdf_reports/guang-da-zheng-quan-zhong-xin-guo-ji-2025-nian-yi-ji'),
    ('pdf_reports/guo-xin-zheng-quan-gong-ye-yu-qi-che-chu-di-fan')
)
SELECT c.doc_key,
       count(*) AS active_chunks,
       count(*) FILTER (WHERE c.effective_date BETWEEN 20250101 AND 20251231
                          AND c.date_confidence = 'explicit') AS in_2025_explicit,
       min(c.effective_date) AS min_date,
       max(c.effective_date) AS max_date
FROM chunks c
JOIN doc_versions v ON v.version_id = c.version_id AND v.doc_key = c.doc_key
WHERE c.status = 'active' AND v.status = 'active'
  AND c.doc_key IN (SELECT doc_key FROM want)
GROUP BY c.doc_key
ORDER BY c.doc_key;

\echo '=== C. 逐题：expected_doc_key 是否 active（B07/B08 用的都在此列） ==='
SELECT v.doc_key, v.status, v.version_id
FROM doc_versions v
WHERE v.doc_key IN (
  'pdf_reports/zhong-yuan-zheng-quan-chan-neng-li-yong-lv-xian-zhu-ti',
  'pdf_reports/dong-fang-zheng-quan-chan-neng-li-yong-lv-ti-sheng-chi',
  'pdf_reports/guang-da-zheng-quan-zhong-xin-guo-ji-2025-nian-yi-ji',
  'pdf_reports/guo-xin-zheng-quan-gong-ye-yu-qi-che-chu-di-fan'
)
ORDER BY v.doc_key, v.status;
