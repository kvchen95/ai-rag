-- ============================================================================
-- 002_ingest_job_status.sql — ingest_jobs 状态机定稿（A1）
--
-- 背景：001_init.sql 有意没给 ingest_jobs.status 加 CHECK，把枚举留给摄取卡（A1）定。
-- 状态机定稿如下，这里把它落成约束——状态一旦只在文档里，就迟早有人在代码里
-- 写一个 'done'，而库里照收。
--
--   状态       含义                         进入方式
--   ---------  ---------------------------  ------------------------------------
--   queued     已入队，等 worker 取          扫描新建 / running 重试退避回来
--   running    正在跑（必须有 started_at）   worker claim_next_job
--   succeeded  跑完（必须有 finished_at）    worker succeed_job
--   failed     终态失败（必须有 finished_at 且必须有 error）worker fail_job / 重试耗尽
--   canceled   人工取消（必须有 finished_at）worker/运维 cancel_job
--
-- 为什么 failed 不再回头：重试是 running -> queued（retry_count+1），不改终态行。
-- 否则「这条任务失败了几次」会被后续改动抹平，审计时就说不清「重试计数」到底
-- 是次数还是尝试状态。
-- ============================================================================

-- 枚举：与 api/ingest/jobs.py 的 JobStatus 逐值对应，改一处必须改另一处。
ALTER TABLE ingest_jobs
    ADD CONSTRAINT ck_ingest_jobs_status
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'canceled'));

-- running 没有开始时间 = 无法判断是否卡住（worker 崩了要按超时判失败，§4.4）。
ALTER TABLE ingest_jobs
    ADD CONSTRAINT ck_ingest_jobs_running_has_started
        CHECK (status <> 'running' OR started_at IS NOT NULL);

-- 终态必须带结束时间：回收/对账/统计"多久跑完"都只看 finished_at。
ALTER TABLE ingest_jobs
    ADD CONSTRAINT ck_ingest_jobs_terminal_has_finished
        CHECK (status NOT IN ('succeeded', 'failed', 'canceled') OR finished_at IS NOT NULL);

-- 失败必须能说出原因：没有 error 的 failed 行在排障时等于没记（§11.3 同一精神）。
ALTER TABLE ingest_jobs
    ADD CONSTRAINT ck_ingest_jobs_failed_has_error
        CHECK (status <> 'failed' OR error IS NOT NULL);

-- 同一个版本同时只能有一个未结束任务。
-- 重复扫描靠 file_hash 判重就已经不会入队两次，这条是兜底：并发扫描、重放、
-- worker 重投都不会让同一版本被解析两遍（两遍会写出两份切片）。
CREATE UNIQUE INDEX uq_ingest_jobs_open_version
    ON ingest_jobs (version_id)
    WHERE status IN ('queued', 'running') AND version_id IS NOT NULL;

-- worker 取任务的形状：只扫 queued，先进先出。ix_ingest_jobs_open 同时含
-- queued 与 running（判断"有没有在跑"用），取任务用这个更窄的。
CREATE INDEX ix_ingest_jobs_queued ON ingest_jobs (created_at) WHERE status = 'queued';

COMMENT ON COLUMN ingest_jobs.status IS
    'queued / running / succeeded / failed / canceled（状态机与重试计数见 api/ingest/jobs.py）';
COMMENT ON COLUMN ingest_jobs.retry_count IS
    'refused 次数：running -> queued 时 +1；超过 INGEST_MAX_RETRIES 直接判 failed(retry_exhausted)';
