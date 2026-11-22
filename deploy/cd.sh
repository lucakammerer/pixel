#!/bin/sh
# One pass of the host's own deploy: origin/main into the live checkout once it is green, in the safe window.
# pixel-cd.timer runs it every 30 s as root; git, uv and the tests run as the user pixel. Root runs only what was
# installed by hand: this script as /opt/pixel/cd/cd.sh and the units in /etc/systemd/system. A merge never changes
# them, so whoever can merge does not get root.
#   PIXEL_CD_DRY=1   logs what it would do and changes nothing in the live checkout or the service
#   touch /opt/pixel/cd/PAUSE   pauses it; rm to resume
#   touch /opt/pixel/cd/SKIP_GATE   lets the newest commit past the replay gate once
set -u
umask 022
CD=/opt/pixel/cd
APP=/opt/pixel/app
REPO=$CD/repo
DRY=${PIXEL_CD_DRY:-0}
ROUND_S=360

exec 9> "$CD/lock"
flock -n 9 || exit 0

as() { runuser -u pixel -- env -i HOME=/opt/pixel PATH=/opt/pixel/.local/bin:/usr/bin:/bin "$@"; }
in_app() { as sh -c "cd $APP && $1"; }
# A line per decision; the same decision on the next pass is not logged again.
log() {
  [ "$DRY" = 1 ] && set -- "dry: $*"
  [ "$*" = "$(cat "$CD/last" 2> /dev/null)" ] && return
  echo "$*" > "$CD/last"
  echo "$(date -u '+%m-%d %H:%M:%S') $*" >> "$CD/cd.log"
}
alert() {
  log "$*"
  [ "$DRY" = 1 ] && return
  url=$(sed -n 's/^PIXEL_ALERT_URL=//p' "$APP/.env" | tr -d "\"'")
  [ -n "$url" ] || return
  jq -n --arg d "$*" '{key: "pixel-deploy", issues: [{issue: "deploy", detail: $d}]}' \
    | curl -fsS -m 10 -o /dev/null -H 'Content-Type: application/json' -d @- "$url" || log "alert did not go out"
}
short() { echo "$1" | cut -c1-7; }
answers() {
  ui=$(systemctl cat pixel | sed -n 's|.*--host \([^ ]*\) --port \([0-9]*\).*|http://\1:\2/|p' | tail -1)
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 2
    systemctl -q is-active pixel && curl -fsS -m 5 -o /dev/null "$ui" && return 0
  done
  return 1
}
# Moves the live checkout to $1 (only ever a commit from main that passed) and syncs it.
move() {
  as git -C "$APP" cat-file -e "$1^{commit}" 2> /dev/null || as git -C "$APP" fetch -q "$REPO" "$1" || return 1
  as git -C "$APP" reset -q --keep "$1" || return 1
  [ "${2:-}" = sync ] || return 0
  in_app "uv sync --locked --no-dev -q"
}
# The live commit is not in $new: deployed by hand (tools/deploy.sh) and not merged yet, or $new is older than it.
# After 10 minutes main wins: a branch rebased after its hand deploy never puts that commit into main.
behind() {
  head=$(as git -C "$APP" rev-parse HEAD)
  as git -C "$REPO" merge-base --is-ancestor "$head" "$new" 2> /dev/null && return 1
  grep -q "^$head " "$CD/behind" 2> /dev/null || echo "$head $(date +%s)" > "$CD/behind"
  [ $(($(date +%s) - $(cut -d' ' -f2 "$CD/behind"))) -lt 600 ]
}
differs() {
  # What tools/replay_gate.py holds against the commit in $REPO, nothing when it passes. A difference the live commit
  # shows too is not this commit's: a change let through by hand stays in the last accepted requests for hours.
  db=$(in_app ".venv/bin/python -c 'import os; from pixel.config import settings; print(os.path.abspath(settings().db_path))'")
  as sh -c "cd $REPO && uv run python -m tools.replay_gate '$db'" > "$CD/gate.out" 2>&1 && return
  grep -q ' differs: ' "$CD/gate.out" || { echo "the replay gate failed: $(tail -1 "$CD/gate.out")"; return; }
  in_app ".venv/bin/python -m tools.replay_gate" 2> /dev/null | grep ' differs: ' > "$CD/gate.live"
  grep ' differs: ' "$CD/gate.out" | grep -vxFf "$CD/gate.live"
}
# A change to these on main takes effect only once installed by hand; say so instead of installing it.
pinned() {
  cmp -s "$APP/deploy/cd.sh" "$CD/cd.sh" || log "deploy/cd.sh on main differs from $CD/cd.sh, install it by hand"
  cmp -s "$APP/deploy/pixel.service" /etc/systemd/system/pixel.service \
    || log "deploy/pixel.service on main differs from the installed unit, install it by hand"
}

[ -e "$CD/PAUSE" ] && { log "paused"; exit 0; }
as git -C "$REPO" fetch -q origin main || { log "fetch failed"; exit 0; }
new=$(as git -C "$REPO" rev-parse origin/main)
live=$(as git -C "$APP" rev-parse HEAD)
[ "$new" = "$live" ] && { log "live is main $(short "$new")"; exit 0; }
behind && { log "live $(short "$live") is not in main $(short "$new"), waiting for its merge"; exit 0; }
grep -qx "$new" "$CD/red" 2> /dev/null && { log "$(short "$new") refused earlier, waiting for a newer commit"; exit 0; }

# Paths that cannot change the running service: move the checkout, no test, no restart.
if changed=$(as git -C "$REPO" diff --name-only "$live" "$new"); then
  restart=0
  for f in $changed; do
    case $f in
      docs/* | bench/* | tests/* | .github/* | deploy/cd.sh | deploy/pixel-cd.* | tools/cd_status.py | tools/replay_gate.py) ;;
      */*) restart=1 ;;
      *.md) ;;
      *) restart=1 ;;
    esac
  done
  if [ "$restart" = 0 ]; then
    [ "$DRY" = 1 ] || move "$new" || { log "could not move to $(short "$new")"; exit 0; }
    log "$(short "$live")..$(short "$new") no restart ($(echo $changed | wc -w) files)"
    [ "$DRY" = 1 ] || pinned
    exit 0
  fi
fi

if ! grep -qx "$new" "$CD/green" 2> /dev/null; then
  log "testing $(short "$new")"
  as git -C "$REPO" checkout -q --detach "$new"
  if as sh -c "cd $REPO && uv sync --locked -q && uv run pytest -q && uv run ruff check ." > "$CD/test.out" 2>&1; then
    echo "$new" >> "$CD/green"
  else
    echo "$new" >> "$CD/red"
    alert "RED $(short "$new"), not deployed: $(grep -E 'passed|failed|error|Found' "$CD/test.out" | tail -1)"
    exit 0
  fi
fi

if [ -e "$CD/SKIP_GATE" ]; then
  rm -f "$CD/SKIP_GATE"
  echo "$new" >> "$CD/gate.green"
  log "$(short "$new") passes the replay gate by hand"
fi
if ! grep -qx "$new" "$CD/gate.green" 2> /dev/null; then
  grep -qx "$new" "$CD/gate.red" 2> /dev/null \
    && { log "$(short "$new") refused by the replay gate, waiting for a newer commit or $CD/SKIP_GATE"; exit 0; }
  found=$(differs)
  if [ -n "$found" ]; then
    echo "$new" >> "$CD/gate.red"
    alert "RED $(short "$new"), not deployed: $(echo "$found" | head -1) ($(echo "$found" | grep -c .) in all)"
    exit 0
  fi
  echo "$new" >> "$CD/gate.green"
fi

# URGENT in a merge since the live commit: no limit of one restart per round, and no wait for the request's first
# two minutes; the answer to the current request must still be out.
urgent=
as git -C "$REPO" log --format=%B "$live..$new" | grep -q URGENT && urgent=--urgent
last=$(cat "$CD/restarted" 2> /dev/null || echo 0)
if [ $(($(date +%s) - last)) -lt "$ROUND_S" ] && [ -z "$urgent" ]; then
  log "$(short "$new") green, waiting: one restart per round"
  exit 0
fi
log "$(short "$new") green, waiting for the window${urgent:+ (urgent)}"
in_app ".venv/bin/python -m tools.window $urgent" || { log "$(short "$new") green, no window within 5 minutes"; exit 0; }
if [ "$DRY" = 1 ]; then
  log "would deploy $(short "$live")..$(short "$new") now"
  exit 0
fi

behind && { log "$(short "$new") does not hold the commit deployed meanwhile, not deployed"; exit 0; }
date +%s > "$CD/restarted"
if move "$new" sync && systemctl restart pixel && answers; then
  log "deployed $(short "$live")..$(short "$new"), restarted once"
  pinned
  exit 0
fi
echo "$new" >> "$CD/red"
move "$live" sync && systemctl restart pixel && answers \
  && alert "$(short "$new") did not start, rolled back to $(short "$live")" \
  || alert "$(short "$new") did not start and the rollback to $(short "$live") did not answer either"
