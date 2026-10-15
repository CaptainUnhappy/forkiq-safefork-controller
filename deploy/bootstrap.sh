#!/bin/sh
set -eu

# ForkIQ initial provisioning; refuses to touch an existing installation.
for path in /opt/forkiq /etc/forkiq /var/lib/forkiq /etc/systemd/system/forkiq.service; do
    if test -e "$path"; then
        printf 'Refusing existing target: %s\n' "$path" >&2
        exit 1
    fi
done
if getent passwd forkiq >/dev/null; then
    printf 'Refusing an existing service account\n' >&2
    exit 1
fi
command -v python3 >/dev/null
command -v systemctl >/dev/null

useradd --system --user-group --home-dir /var/lib/forkiq --shell /usr/sbin/nologin forkiq
install -d -o forkiq -g forkiq -m 0700 /var/lib/forkiq
install -d -o root -g root -m 0755 /opt/forkiq
# The App private key is created later by the manifest callback (0600, service user).
touch /var/lib/forkiq/app.pem
chown forkiq:forkiq /var/lib/forkiq/app.pem
chmod 0600 /var/lib/forkiq/app.pem

cd /opt/forkiq
python3 -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
chown -R root:root /opt/forkiq
chmod -R go-w /opt/forkiq

# The service binds loopback; no public or Docker bridge firewall rule is needed.
printf 'Provisioned ForkIQ account and directories. Install code, then: systemctl daemon-reload && systemctl enable --now forkiq.service\n'
