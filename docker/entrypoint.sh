#!/bin/sh
# Run as PUID:PGID (like linuxserver images). Keep the GPU device groups (render) as supplementary groups.
set -e
PUID=${PUID:-1000}
PGID=${PGID:-1000}
mkdir -p /config/logs

if [ "$(id -u)" != "0" ]; then
  exec "$@"
fi

chown "$PUID:$PGID" /config /config/logs
[ -f /config/reencoder.db ] && chown "$PUID:$PGID" /config/reencoder.db* 2>/dev/null || true
chown -R "$PUID:$PGID" /config/logs 2>/dev/null || true

groups="$PGID"
for dev in /dev/dri/*; do
  [ -e "$dev" ] || continue
  g=$(stat -c %g "$dev")
  case ",$groups," in *",$g,"*) ;; *) groups="$groups,$g" ;; esac
done
# Also keep any groups added with docker's group_add.
for g in $(id -G); do
  [ "$g" = "0" ] && continue
  case ",$groups," in *",$g,"*) ;; *) groups="$groups,$g" ;; esac
done

export HOME=/config
exec setpriv --reuid="$PUID" --regid="$PGID" --groups="$groups" --inh-caps=-all -- "$@"
