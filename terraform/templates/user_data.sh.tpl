#!/bin/bash
# First-boot bootstrap for the ECS EC2 launch-type host. Rendered by
# terraform/ec2.tf via templatefile() - the cluster name and the rendered
# nginx config are substituted in below. NOTE: never write a real
# dollar-brace placeholder in a comment like this one - templatefile()
# substitutes those sequences anywhere in the file, even inside a "#"
# comment, and only the first line of a multi-line substitution stays
# commented; the rest becomes unprotected raw bash.
set -eux

# --- Join the existing ECS cluster (aws_ecs_cluster.main) ---
echo "ECS_CLUSTER=${cluster_name}" >> /etc/ecs/ecs.config

# --- nginx: reverse proxy to voice_gateway on 127.0.0.1:8001 ---
dnf install -y nginx
systemctl enable --now nginx

cat > /etc/nginx/conf.d/app.conf <<'NGINX_EOF'
${nginx_conf}
NGINX_EOF

nginx -t
systemctl reload nginx

# --- certbot: installed and ready, but NOT run yet. The ACME HTTP-01
# challenge needs the domain's DNS record already resolving to this
# instance's Elastic IP, which can't happen until after this apply and
# the Route53 A record is added - see terraform/README.md's "one-time,
# manual" certbot step. AL2023 has no amazon-linux-extras, so install via
# pip into a dedicated venv, per certbot's own recommendation for
# RPM-family distros without snapd. ---
dnf install -y python3 augeas-libs
python3 -m venv /opt/certbot
/opt/certbot/bin/pip install --upgrade pip
/opt/certbot/bin/pip install certbot certbot-nginx
ln -sf /opt/certbot/bin/certbot /usr/bin/certbot

# --- Auto-renewal via a systemd timer, not cron - this AMI has no cron
# package installed at all (no crontab/crond/cron.d), and a timer is the
# native AL2023/systemd-only way to do this without adding one. Installed
# now so it's ready the moment the one-time manual `certbot --nginx -d ...`
# step (done after DNS resolves) issues the first real cert - renewal
# before that just has nothing to renew yet, which is harmless. ---
cat > /etc/systemd/system/certbot-renew.service <<'UNIT_EOF'
[Unit]
Description=Certbot renewal

[Service]
Type=oneshot
ExecStart=/usr/bin/certbot renew --quiet --deploy-hook "systemctl reload nginx"
UNIT_EOF

cat > /etc/systemd/system/certbot-renew.timer <<'UNIT_EOF'
[Unit]
Description=Run certbot renew twice daily

[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=3600
Persistent=true

[Install]
WantedBy=timers.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now certbot-renew.timer

echo "user-data bootstrap complete" > /var/log/user-data-done.log
