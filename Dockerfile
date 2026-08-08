FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends procps && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir requests psutil

# код в /opt/image — при первом старте копируется на volume /opt/intel
WORKDIR /opt/image
COPY scripts/ /opt/image/scripts/
COPY entrypoint.sh /opt/image/entrypoint.sh
RUN chmod +x /opt/image/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/intel

ENTRYPOINT ["/opt/image/entrypoint.sh"]