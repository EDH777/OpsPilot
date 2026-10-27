# Python 沙箱镜像（强隔离模式）
#
# 构建：
#   docker build -f infra/sandbox.Dockerfile -t opspilot-sandbox:latest .
# 启用：
#   $env:OPSPILOT_SANDBOX="docker"
#
# 运行时会以 --network none、--memory 1g、--cpus 1、非 root 用户启动，
# 工作目录挂载为 /work。
FROM python:3.11-slim

RUN pip install --no-cache-dir \
    "pandas>=2.0" \
    "numpy>=1.26" \
    "matplotlib>=3.8" \
    "duckdb>=1.0"

RUN useradd -m -u 10001 sandbox
WORKDIR /work
RUN chown -R sandbox:sandbox /work
USER sandbox

ENV MPLBACKEND=Agg \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1
