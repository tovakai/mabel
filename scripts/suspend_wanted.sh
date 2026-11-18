#!/bin/sh
# hypridle's suspend_cmd. Notes that hypridle wanted to sleep, then suspends.
# If Mabel's night shift holds sleep, the suspend fails here; the note tells her
# (src/heartbeat/power.py hypridle_wanted_sleep) to suspend once her jobs are done.
# A movie or anything else that inhibits hypridle never gets here, so she leaves those alone.
state="${XDG_STATE_HOME:-$HOME/.local/state}/mabel"
mkdir -p "$state"
date +%s > "$state/suspend-wanted.tmp" && mv "$state/suspend-wanted.tmp" "$state/suspend-wanted"
systemctl suspend || loginctl suspend
