#!/bin/bash
set -e

DOVECOT_USER="${DOVECOT_USER:-testuser}"
DOVECOT_PASSWORD="${DOVECOT_PASSWORD:-testpass}"

# Create the system user for mail storage if needed
if ! id "$DOVECOT_USER" &>/dev/null; then
    useradd -m -s /bin/false "$DOVECOT_USER"
fi

mkdir -p "/home/${DOVECOT_USER}/Maildir"
chown -R "$DOVECOT_USER:$DOVECOT_USER" "/home/${DOVECOT_USER}/Maildir"

# Passwd file — plain text password, no scheme prefix when using scheme=PLAIN in passdb args
printf '%s:%s\n' "$DOVECOT_USER" "$DOVECOT_PASSWORD" > /etc/dovecot/users
chown root:dovecot /etc/dovecot/users
chmod 640 /etc/dovecot/users

# Replace the stock 10-auth.conf so we're the only passdb/userdb
cat > /etc/dovecot/conf.d/10-auth.conf << EOF
disable_plaintext_auth = no
auth_mechanisms = plain login

passdb {
  driver = passwd-file
  args = scheme=PLAIN /etc/dovecot/users
}

userdb {
  driver = passwd
}
EOF

# Drop a local override for all other key settings
cat > /etc/dovecot/local.conf << EOF
ssl = no
protocols = imap
listen = *
mail_location = maildir:~/Maildir
log_path = /dev/stderr
info_log_path = /dev/stderr

service imap-login {
  inet_listener imap {
    port = 143
    ssl = no
  }
  inet_listener imaps {
    port = 0
  }
}

namespace inbox {
  inbox = yes
  separator = /
  prefix =
}
EOF

echo "Dovecot starting as user=${DOVECOT_USER} on port 143..."
exec /usr/sbin/dovecot -F
