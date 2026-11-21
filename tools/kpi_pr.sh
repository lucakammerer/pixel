#!/bin/sh
# Measure a pull request and its base on the live host, online, on a copy of its database, and post the comparison
# into the pull request. It reads portal rates on the live key's budget and spends through the gateway (R28):
# start it right after a quote went out, or the next live request waits for its rates.
set -eu
pr=$1
host=${PIXEL_HOST:-luca-kammerer}
out=$(mktemp -d)
remote='d=$(mktemp -d) && tar -x -C $d && cd $d && set -a && . /opt/pixel/app/.env && set +a && /opt/pixel/.local/bin/uv run -q --locked --no-dev python -m tools.kpi --online --db /opt/pixel/app/data/pixel.sqlite3; s=$?; rm -rf $d; exit $s'
measure() { git archive "$1" | ssh "$host" "sudo -u pixel -H sh -c '$remote'"; }
set -- $(gh pr view "$pr" --json baseRefOid,headRefOid --jq '.baseRefOid, .headRefOid')
git fetch -q origin "$1" "$2"
measure "$1" > "$out/base.json" || : > "$out/base.json"  # a base without the tool: the branch alone
measure "$2" > "$out/branch.json"
uv run python -m tools.kpi --report "$out/branch.json" --base "$out/base.json" > "$out/kpi.md"
gh pr comment "$pr" --body-file "$out/kpi.md"
