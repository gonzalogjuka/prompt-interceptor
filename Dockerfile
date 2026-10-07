FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir /data && chown 65534:65534 /data
COPY interceptor ./interceptor
COPY routes.json perfil.md ./
# settings: docker run --env-file config.env [--env-file .env] ...
ENV HOST=0.0.0.0 PORT=8787 METRICS_PATH=/data/metrics.jsonl
VOLUME /data
EXPOSE 8787
USER 65534
CMD ["python", "-m", "interceptor.app"]
