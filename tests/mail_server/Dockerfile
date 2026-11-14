# A real IMAP and SMTP server for tests/unit/test_mail_real_server.py: Dovecot and Postfix, TLS only.
FROM alpine:3.20
RUN apk add --no-cache dovecot dovecot-lmtpd postfix openssl
COPY dovecot.conf /etc/dovecot/dovecot.conf
COPY main.cf /etc/postfix/main.cf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
 && printf '%s\n' \
    'smtps inet n - n - - smtpd -o smtpd_tls_wrappermode=yes -o smtpd_sasl_auth_enable=yes' \
    'submission inet n - n - - smtpd -o smtpd_tls_security_level=encrypt -o smtpd_tls_auth_only=yes -o smtpd_sasl_auth_enable=yes' \
    >> /etc/postfix/master.cf
EXPOSE 143 993 465 587
ENTRYPOINT ["/entrypoint.sh"]
