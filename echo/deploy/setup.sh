#!/bin/bash
# setup.sh — Eenmalig uitvoeren als root op een verse Ubuntu 22.04 / Debian 12 VPS
# Gebruik: bash <(curl -s https://raw.githubusercontent.com/twetering/stemmy-cli/main/echo/deploy/setup.sh)
# Of na SSH: sudo bash echo/deploy/setup.sh

set -e
echo ""
echo "  ⬛ GRAFSTEMMING — server setup"
echo "  ──────────────────────────────"

# 1. Systeem-packages
echo "→ Packages installeren..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    ffmpeg nginx certbot python3-certbot-nginx \
    git curl

# 2. Gebruiker aanmaken
echo "→ Gebruiker 'stemmy' aanmaken..."
id stemmy &>/dev/null || useradd -m -s /bin/bash stemmy

# 3. Mappen
echo "→ Mappen aanmaken..."
mkdir -p /opt/stemmy /data
chown stemmy:stemmy /opt/stemmy /data

# 4. Repo clonen (of updaten)
echo "→ Repository ophalen..."
if [ -d /opt/stemmy/.git ]; then
    sudo -u stemmy git -C /opt/stemmy pull
else
    sudo -u stemmy git clone https://github.com/twetering/stemmy-cli.git /opt/stemmy
fi

# 5. Python virtualenv
echo "→ Python omgeving bouwen..."
sudo -u stemmy python3 -m venv /opt/stemmy/.venv
sudo -u stemmy /opt/stemmy/.venv/bin/pip install --quiet -e /opt/stemmy

# 6. Cache-map (leeg, wordt gevuld tijdens gebruik)
mkdir -p /opt/stemmy/echo/cache
chown stemmy:stemmy /opt/stemmy/echo/cache

# 7. Systemd service
echo "→ Systemd service installeren..."
cp /opt/stemmy/echo/deploy/stemmy-echo.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable stemmy-echo

# 8. Nginx
echo "→ Nginx configureren..."
cp /opt/stemmy/echo/deploy/nginx-echo.conf /etc/nginx/sites-available/echo.stemmy.nl
ln -sf /etc/nginx/sites-available/echo.stemmy.nl /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

echo ""
echo "  ✅ Setup klaar! Volgende stappen:"
echo ""
echo "  STAP 1 — Database uploaden (vanaf je Mac):"
echo "    scp stemmy-replica.db root@<SERVER-IP>:/data/"
echo "    ssh root@<SERVER-IP> 'chown stemmy:stemmy /data/stemmy-replica.db'"
echo ""
echo "  STAP 2 — Server starten:"
echo "    systemctl start stemmy-echo"
echo "    systemctl status stemmy-echo"
echo "    journalctl -u stemmy-echo -f   # logs bekijken"
echo ""
echo "  STAP 3 — HTTPS (nadat DNS klaar is):"
echo "    certbot --nginx -d echo.stemmy.nl"
echo ""
echo "  DNS (in cloud86.io cPanel):"
echo "    Type: A"
echo "    Naam: echo"
echo "    Waarde: $(curl -s ifconfig.me)"
echo ""
