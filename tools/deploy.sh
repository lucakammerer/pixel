#!/bin/sh
# Deploy the checked-out commit to the live host and restart the service. Run it between two requests.
set -eu
host=${PIXEL_HOST:-luca-kammerer}
git push --receive-pack='sudo -u pixel git-receive-pack' "$host:/opt/pixel/app" HEAD:main
ssh "$host" 'sudo -u pixel -H sh -c "cd /opt/pixel/app && /opt/pixel/.local/bin/uv sync --locked --no-dev -q" \
  && sudo install -m 644 /opt/pixel/app/deploy/pixel.service /etc/systemd/system/pixel.service \
  && sudo systemctl daemon-reload && sudo systemctl restart pixel && sleep 3 && systemctl is-active pixel'
