# ai-rag API 镜像。
#
# 三个 stage：
#   base  —— 运行时依赖（compose 里 api 服务挂载宿主源码，镜像只提供解释器与依赖）
#   dev   —— 追加 pytest/ruff，用来跑 api/tests
#   prod  —— 默认 stage（放最后：docker build 不带 --target 时取最后一个）
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# curl 供 compose healthcheck 使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY infra/api/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

# 不挂载源码也能单独跑：api + schema 迁移脚本一起进镜像。
# （compose 里 ../:/app 会覆盖这些路径，用的是宿主上的同一份代码。）
COPY api /app/api
COPY db /app/db
COPY scripts /app/scripts
# 配置清单进镜像：api/tests 里有一条"代码要求的变量 .env.example 是否都写了"的
# 检查，缺这个文件就只能在宿主上跑，容器里跑测试会静默少一条判据。
COPY .env.example /app/.env.example
COPY infra/api/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]


FROM base AS dev

COPY infra/api/requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install -r /tmp/requirements-dev.txt


FROM base AS prod

# --factory：app 由 create_app() 装配，不在 import 时连库（见 api/main.py）
CMD ["uvicorn", "api.main:create_app", "--factory", "--app-dir", "/app", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
