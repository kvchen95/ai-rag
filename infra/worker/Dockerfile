FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# worker 复用 api 的依赖清单：它 import api.ingest.* / api.clients.* / api.config，
# 两份 requirements.txt 迟早出现"worker 里少一个包"这类隐性故障（A1 的 worker
# requirements 里 qdrant-client 还是 1.12，而服务端是 1.15）。
COPY infra/api/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

# 摄取 worker 要真解析 PDF（api.parse.pipeline -> pdfplumber/pypdfium2/Pillow），
# 这一份依赖只在 dev stage 装过，运行时镜像必须自己带上，否则 ingest-once
# 一 import 就 ModuleNotFoundError。在线 api 镜像保持轻量，不装这些。
COPY infra/api/requirements-parse.txt /tmp/requirements-parse.txt
RUN pip install -r /tmp/requirements-parse.txt

# 源码由 compose 挂载（../:/app），镜像里不放代码：改代码不用重建镜像。
# 默认入口是 vector_sync 常驻循环（§4.2：翻牌后的期望状态必须有东西消费）。
# 摄取 worker 由 compose 的独立 ingest-worker 服务跑（同一镜像，command 覆盖），
# 不塞进同一个 PID：两者的失败模式与日志完全不同，分开才好重启与排障。
CMD ["python", "worker/vector_sync_worker.py"]
