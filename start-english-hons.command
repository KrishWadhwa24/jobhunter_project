#!/bin/bash
# Double-click in Finder: sets this profile up (first time) and starts it. The profile is the part after "start-".
cd "$(dirname "$0")" || exit 1
slug=$(basename "$0" .command); slug=${slug#start-}
./jh "$slug" setup || { read -r -p "Setup failed. Press Enter to close"; exit 1; }
./jh "$slug" restart
sleep 2
./jh "$slug" open
echo; echo "Running. Stop it with:  ./jh $slug stop"
read -r -p "Press Enter to close this window (it keeps running)"
