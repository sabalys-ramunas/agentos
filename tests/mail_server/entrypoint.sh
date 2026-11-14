#!/bin/sh
# A CA and a server certificate for 127.0.0.1 only, so `localhost` is the wrong name for it, then both servers.
set -e
dir=/etc/ssl/mailtest
mkdir -p "$dir"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=agentos mailtest CA" \
  -keyout "$dir/ca.key" -out "$dir/ca.crt" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=127.0.0.1" -keyout "$dir/server.key" -out "$dir/server.csr" 2>/dev/null
printf 'subjectAltName=IP:127.0.0.1\n' > "$dir/san.cnf"
openssl x509 -req -days 2 -in "$dir/server.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" -CAcreateserial \
  -extfile "$dir/san.cnf" -out "$dir/server.crt" 2>/dev/null
chmod 644 "$dir/server.key"


mkdir -p /var/mail && chown vmail:postdrop /var/mail
cat > /etc/dovecot/users <<'USERS'
owner@example.test:{PLAIN}right-password-1
friend@example.test:{PLAIN}friend-password-2
utf8@example.test:{PLAIN}pässwörd-3
USERS

newaliases 2>/dev/null || true
postfix start-fg &
exec dovecot -F
