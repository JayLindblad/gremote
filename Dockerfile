FROM python:3.12-slim

# git: the in-container `update` command pulls the latest code from GitHub.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/gremote
COPY . .
RUN pip install --no-cache-dir -e . \
 && printf '#!/bin/sh\nexec bash /opt/gremote/deploy/docker-update.sh "$@"\n' > /usr/bin/update \
 && chmod 0755 /usr/bin/update

ENV GREMOTE_DATABASE_PATH=/data/gremote.db
VOLUME /data
EXPOSE 8000
# Runs the app and restarts it after `update`; settings live in /data/gremote.env.
ENTRYPOINT ["bash", "/opt/gremote/deploy/docker-entrypoint.sh"]
