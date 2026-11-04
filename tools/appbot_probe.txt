#!/data/data/com.termux/files/usr/bin/sh
# Probe: can the phone drive its own UI (uiautomator + input) through a
# local adb connection to its own adbd?  This is the foundation of the
# app-based bot - no login, no Cloudflare, no protocol reverse engineering.
PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PREFIX/bin:$PATH"
export HOME=$PREFIX/files/home
OUT=$PREFIX/tmp/appbot_probe.txt
mkdir -p "$PREFIX/tmp"

{
echo "=== install android-tools ==="
pkg install -y android-tools 2>&1 | tail -6
echo "=== adb version ==="
adb version 2>&1 | head -2
echo "=== self connect to own adbd ==="
adb connect 127.0.0.1:5555 2>&1 | tail -2
adb devices 2>&1 | tail -4
echo "=== shell test ==="
adb -s 127.0.0.1:5555 shell echo SHELL_OK 2>&1 | tail -2
echo "=== launch wolvesville app ==="
adb -s 127.0.0.1:5555 shell monkey -p com.werewolfapps.online -c android.intent.category.LAUNCHER 1 2>&1 | tail -2
sleep 14
echo "=== ui dump (truncated) ==="
adb -s 127.0.0.1:5555 exec-out uiautomator dump /dev/tty 2>&1 | head -c 3000
echo
echo "=== done ==="
} > "$OUT" 2>&1
