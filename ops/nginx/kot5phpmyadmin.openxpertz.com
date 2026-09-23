# kot5phpmyadmin.openxpertz.com -> phpMyAdmin of the kot5 XPBuilder stack.
#
# The phpMyAdmin container publishes on the loopback interface only
# (XPBUILDER_PHPMYADMIN_HOST_PORT, default 9011 for kot5), so this vhost is the
# single public entry point and TLS is therefore mandatory. Port 9011 below must
# match XPBUILDER_PHPMYADMIN_HOST_PORT in the site .env.
#
# Install (see ops/README.md):
#   install -m 0644 ops/nginx/kot5phpmyadmin.openxpertz.com \
#       /etc/nginx/sites-available/kot5phpmyadmin.openxpertz.com
#   ln -s ../sites-available/kot5phpmyadmin.openxpertz.com \
#       /etc/nginx/sites-enabled/kot5phpmyadmin.openxpertz.com
#   nginx -t && systemctl reload nginx
#   certbot certonly --webroot -w /var/www/letsencrypt \
#       -d kot5phpmyadmin.openxpertz.com
#   certbot --nginx -d kot5phpmyadmin.openxpertz.com --redirect
server {
    listen 80;
    server_name kot5phpmyadmin.openxpertz.com;

    # Large .sql imports: keep in step with the container's UPLOAD_LIMIT.
    client_max_body_size 512M;

    # ACME HTTP-01 challenges (certbot --webroot)
    location /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
    }

    location / {
        proxy_pass http://127.0.0.1:9011;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Imports and exports of large tables must not hit the 60s default.
        proxy_connect_timeout 60s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        client_body_timeout 600s;
    }

    access_log /var/log/nginx/kot5phpmyadmin.openxpertz.com.access.log;
    error_log  /var/log/nginx/kot5phpmyadmin.openxpertz.com.error.log;
}
