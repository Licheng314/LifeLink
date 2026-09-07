FROM python:3.13-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin lifelink \
    && mkdir -p /var/lib/lifelink \
    && chown -R lifelink:lifelink /var/lib/lifelink

WORKDIR /opt/lifelink

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LIFE_LINK_DATA_ROOT=/var/lib/lifelink \
    LIFE_RADIO_CENTRAL_HOST=0.0.0.0 \
    LIFE_RADIO_CENTRAL_PORT=8091 \
    LIFE_LINK_CONTAINER_MANAGEMENT=1 \
    LIFE_LINK_MANAGEMENT_HOST=0.0.0.0

COPY --chown=lifelink:lifelink central/ ./central/
COPY --chown=lifelink:lifelink management-web/ ./management-web/
COPY --chown=lifelink:lifelink central_server.py central_windows_startup.py central_endpoint.py configure_tailscale_endpoint.py ./
COPY --chown=lifelink:lifelink docker-entrypoint.sh ./
RUN chmod 0555 docker-entrypoint.sh

USER lifelink
VOLUME ["/var/lib/lifelink"]
EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; assert urlopen('http://127.0.0.1:8091/v1/health', timeout=3).status == 200"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["run"]
