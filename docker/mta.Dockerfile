# RevBridge Connect — MTA: Postfix + OpenDKIM (key read from the database) + Dovecot (SASL only,
# with the API key as the password) + rsyslog. The configuration is rendered at start-up from
# mta/templates with the variables of the .env; no password is stored in the image.
#
# Delivery always goes straight to the MX (or to one of your own nodes, through the
# relays/zone_relays tables): there is no provider relay. Build context = the repository root.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        postfix postfix-mysql libmariadb-dev-compat \
        opendkim opendkim-tools \
        libsasl2-modules \
        lua5.3 liblua5.3-dev \
        dovecot-core dovecot-imapd dovecot-pop3d dovecot-lmtpd dovecot-mysql \
        ca-certificates rsyslog logrotate dnsutils curl \
        python3 python3-jinja2 supervisor \
    && ( apt-get install -y --no-install-recommends lua-sql-mysql5.3 \
         || apt-get install -y --no-install-recommends lua-sql-mysql ) \
    && rm -rf /var/lib/apt/lists/*

# vmail (uid/gid 5000): owner of the folders Dovecot expects
RUN groupadd -g 5000 vmail && useradd -u 5000 -g vmail -m -d /home/vmail -s /usr/sbin/nologin vmail \
    && mkdir -p /var/lib/logrotate

COPY mta/templates /opt/connect/templates
COPY mta/render.py mta/entrypoint.sh mta/tls-sync.sh mta/keytable-inline.lua /opt/connect/
COPY mta/supervisord.conf /etc/supervisor/connect-supervisord.conf
COPY mta/logrotate.conf /etc/logrotate.d/connect-mail
RUN chmod 755 /opt/connect/entrypoint.sh /opt/connect/tls-sync.sh \
    && chmod 644 /etc/logrotate.d/connect-mail

EXPOSE 25 587 465
ENTRYPOINT ["/opt/connect/entrypoint.sh"]
