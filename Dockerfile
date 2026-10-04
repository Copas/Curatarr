FROM python:3.12-slim

# Install the package itself; the source tree is not kept in the image.
COPY pyproject.toml README.md LICENSE /src/
COPY curatarr /src/curatarr
RUN pip install --no-cache-dir /src && rm -rf /src

COPY docker-entrypoint.sh /usr/local/bin/curatarr-entrypoint
RUN chmod +x /usr/local/bin/curatarr-entrypoint \
    && useradd --system --home-dir /config --shell /usr/sbin/nologin curatarr \
    && mkdir -p /config && chown curatarr: /config

ENV CURATARR_HOST=0.0.0.0 CURATARR_PORT=8787 CURATARR_DATA_DIR=/config
WORKDIR /config
USER curatarr
EXPOSE 8787
VOLUME ["/config"]
ENTRYPOINT ["curatarr-entrypoint"]
CMD ["gunicorn", "--bind", "0.0.0.0:8787", "--workers", "1", "curatarr:create_app()"]
