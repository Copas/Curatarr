FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY curatarr ./curatarr
COPY migrations ./migrations
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN pip install --no-cache-dir . && chmod +x /app/docker-entrypoint.sh

ENV CURATARR_HOST=0.0.0.0 CURATARR_PORT=8787 CURATARR_DATA_DIR=/config
EXPOSE 8787
VOLUME ["/config"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8787", "--workers", "1", "curatarr:create_app()"]
