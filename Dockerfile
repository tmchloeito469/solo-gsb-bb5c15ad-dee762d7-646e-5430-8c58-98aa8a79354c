FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

COPY . .

EXPOSE 8000

# 容器启动时安装依赖，然后启动 API 服务
CMD ["sh", "-c", "pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
