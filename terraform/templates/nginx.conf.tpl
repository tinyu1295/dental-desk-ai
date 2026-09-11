# Reverse proxy: TLS-terminating front door for voice_gateway, which
# only ever listens on 127.0.0.1:8001 (never exposed directly - see
# ec2.tf's security group, which has neither container port in it).
# certbot's nginx plugin adds the matching `listen 443 ssl` server block
# + http->https redirect here the first time it's run (see the one-time
# manual certbot step in terraform/README.md).
#
# map lives at this file's top level (outside `server {}`) because
# /etc/nginx/conf.d/*.conf is included inside nginx.conf's `http {}` block,
# where `map` is valid - this makes the Connection header correctly
# fall back to "close" for a normal (non-websocket) request instead of
# always claiming "upgrade".
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name ${domain_name};

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # /media-stream is a long-lived WebSocket for a call's whole
        # duration - the 60s default here would silently truncate real
        # calls.
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
