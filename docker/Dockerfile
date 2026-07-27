FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends dovecot-imapd \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 143

ENTRYPOINT ["/entrypoint.sh"]
