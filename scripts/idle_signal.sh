#!/bin/sh
# Called by hypridle: "idle" after its idle timeout, "active" on resume.
# Mabel's heartbeat reads the state file to know whether Anthon is away.
state="${XDG_STATE_HOME:-$HOME/.local/state}/mabel"
case "$1" in idle|active) ;; *) echo "usage: $0 idle|active" >&2; exit 2 ;; esac
mkdir -p "$state"
printf '%s %s\n' "$1" "$(date +%s)" > "$state/idle.tmp" && mv "$state/idle.tmp" "$state/idle"
