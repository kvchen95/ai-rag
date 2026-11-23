-- ============================================================================
-- 003_vector_sync.sql — 向量库副本的"期望状态"与漂移账（A3）
--
-- 背景（详细设计.md §4.1 / §4.2 / §4.3）
--
--   翻牌在一个事务里改三处：旧版 deprecated、新版 active、切片批量改状态。
--   向量库是副本，必须跟着改，但**它不在那个事务里**——Qdrant 没有事务，也不该
--   被 PG 的事务边界绑住（网络一抖就会把一次成功的翻牌回滚掉）。
--
--   于是把"向量库该长什么样"落成一张表（desired state），而不是靠"翻牌后顺手
--   发个 HTTP 请求"：
--
--     * 翻牌事务里同时写 vector_sync_tasks —— 两件事同生共死。若只在提交后发请求，
--       进程在中间崩掉就会留下"PG 已翻牌、Qdrant 永远不知道"的静默漂移，
--       而且没有任何记录能证明它发生过。
--     * 同步失败重试三次（§4.2）；耗尽后写一条 vector_drift_log，让漂移被**数**出来。
--     * 对账（reconcile）以 PG 为准覆盖 Qdrant，并把每次纠正记进同一张漂移账。
--
-- 为什么一个版本只有一行（version_id 主键）
--
--   这是"期望状态"表，不是任务流水。同一版本在 24h 内可能被反复翻牌
--   （active -> deprecated -> 回滚 active），写流水会引入"两条相反的指令谁先谁后"
--   的问题；写期望状态则永远只有一句"它现在应该是 X"，重放天然幂等。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- doc_versions.deprecated_at：回收窗口的起点（§4.3「保留 24 小时后删除」）
--
-- 001 里的 ix_doc_versions_deprecated_at 建在 activated_at 上——那列记的是**生效**
-- 时间，拿它算下线时长会把"生效了 30 天才被取代"的版本在翻牌瞬间判成已过期，
-- 直接删掉仍在本窗口内的回滚门。这里补一列，把两个时间点分开。
-- ---------------------------------------------------------------------------
ALTER TABLE doc_versions ADD COLUMN deprecated_at timestamptz;

COMMENT ON COLUMN doc_versions.deprecated_at IS
    '被划为 deprecated 的时间；回收窗口从这一刻起算 24h（§4.3）。NULL 表示未记录，回收时按"刚下线"处理';

-- 已有 deprecated 行回填：不确定就按 now() 算，宁可晚删一轮也不能提前删。
UPDATE doc_versions SET deprecated_at = now() WHERE status = 'deprecated' AND deprecated_at IS NULL;

-- 001 那个索引名字对、口径错（建在 activated_at 上），这里换掉。
DROP INDEX IF EXISTS ix_doc_versions_deprecated_at;
CREATE INDEX ix_doc_versions_deprecated_at
    ON doc_versions (deprecated_at) WHERE status = 'deprecated';

-- 刻意**不加** "deprecated 必须带 deprecated_at" 的 CHECK：
-- 其它模块（扫描的单测夹具、回滚脚本）会把状态直接改成 deprecated 而不带时间戳，
-- 加了 CHECK 会让它们的用例红在一处与本卡无关的地方。代价是这类行会让回收窗口
-- 失去起点，处理方式是 reclaim 遇到 NULL 时**先补 now() 并跳过本轮**（见
-- api/ingest/reclaim.py）：未知下线时间 → 从此刻开始计时，绝不"立刻删"。

-- ---------------------------------------------------------------------------
-- vector_sync_tasks：向量库的期望状态（一个版本一行）
-- ---------------------------------------------------------------------------
CREATE TABLE vector_sync_tasks (
    version_id      text        PRIMARY KEY
                                REFERENCES doc_versions (version_id) ON DELETE CASCADE,
    doc_key         text        NOT NULL,
    -- 该版本在向量库里应有的状态：active（可检索）/ deprecated（留着但不可检索）/ purged（点已删）
    desired_status  text        NOT NULL,
    -- 上一次同步成功后写入的状态；只作审计与"改了什么"的证据，不参与判断
    synced_status   text,
    status          text        NOT NULL DEFAULT 'pending',
    attempts        int         NOT NULL DEFAULT 0,
    last_error      text,
    -- 退避：下一次允许尝试的时间。drain 只捞 next_attempt_at <= now() 的行
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    synced_at       timestamptz,
    CONSTRAINT ck_vector_sync_desired
        CHECK (desired_status IN ('active', 'deprecated', 'purged')),
    CONSTRAINT ck_vector_sync_status
        CHECK (status IN ('pending', 'done', 'failed')),
    CONSTRAINT ck_vector_sync_attempts CHECK (attempts >= 0),
    -- 失败必须能说出原因：一条没有 last_error 的 failed 在排障时等于没记（同 002 的口径）
    CONSTRAINT ck_vector_sync_failed_has_error
        CHECK (status <> 'failed' OR last_error IS NOT NULL),
    CONSTRAINT ck_vector_sync_done_has_synced
        CHECK (status <> 'done' OR synced_at IS NOT NULL)
);

COMMENT ON TABLE  vector_sync_tasks IS
    '向量库期望状态：翻牌事务里与状态变更同事务写入，由 sync.drain 消费，失败重试后转 drift';
COMMENT ON COLUMN vector_sync_tasks.desired_status IS
    'active/deprecated=按 version_id 批量改 payload.status；purged=按 version_id 删点';
COMMENT ON COLUMN vector_sync_tasks.status IS
    'pending（待同步）/ done（已同步且校验通过）/ failed（重试耗尽，转人工或由对账修复）';

-- drain 的取数形状：只扫到期的 pending，按 updated_at 先进先出
CREATE INDEX ix_vector_sync_due
    ON vector_sync_tasks (next_attempt_at) WHERE status = 'pending';
CREATE INDEX ix_vector_sync_doc ON vector_sync_tasks (doc_key, status);

-- ---------------------------------------------------------------------------
-- vector_drift_log：每一次"两边不一致"都要留痕（§4.2「漂移本身不可怕，漂移没人数才可怕」）
--
-- 这张表是给对账与告警用的：它是"发生过多少次漂移、修没修好"的唯一来源。
-- chunk_id / version_id 故意不加外键：切片可能已经被回收删掉，而这条漂移记录
-- 仍然要保留（审计追溯的对象是被删掉的东西）。
-- ---------------------------------------------------------------------------
CREATE TABLE vector_drift_log (
    id           bigserial   PRIMARY KEY,
    detected_at  timestamptz NOT NULL DEFAULT now(),
    doc_key      text        NOT NULL,
    chunk_id     text,
    version_id   text,
    kind         text        NOT NULL,
    pg_value     text,
    qdrant_value text,
    fixed        boolean     NOT NULL DEFAULT false,
    fixed_at     timestamptz,
    fix_error    text,
    -- 同一次对账里重复探测到同一处漂移不该刷屏：靠这个键做"已记录未修"的去重
    dedupe_key   text        NOT NULL,
    CONSTRAINT ck_vector_drift_kind
        CHECK (kind IN ('status_mismatch', 'version_mismatch', 'missing_point',
                        'orphan_point', 'sync_failed', 'sync_incomplete'))
);

COMMENT ON TABLE  vector_drift_log IS
    '向量库与 PG 的漂移账：同步失败耗尽、对账发现的不一致都在这里，fixed 表示是否已被纠正';
COMMENT ON COLUMN vector_drift_log.kind IS
    'status_mismatch / version_mismatch / missing_point / orphan_point / sync_failed / sync_incomplete';

CREATE INDEX ix_vector_drift_open ON vector_drift_log (detected_at DESC) WHERE NOT fixed;
CREATE INDEX ix_vector_drift_doc  ON vector_drift_log (doc_key, detected_at DESC);
-- 未修复的同一处漂移只留一行（对账每小时跑一次，否则一天刷 24 条同样的记录）
CREATE UNIQUE INDEX uq_vector_drift_open_dedupe
    ON vector_drift_log (dedupe_key) WHERE NOT fixed;

-- ---------------------------------------------------------------------------
-- 回收的取数形状：status='deprecated' 且已过窗口，按 deprecate 时间先进先出。
-- 单独一条部分索引而不是复用上面那条：回收要 ORDER BY，ix_doc_versions_deprecated_at
-- 只按 (deprecated_at) 排，够用；这里保留注释说明为什么不再加第二条索引。
-- ---------------------------------------------------------------------------
