#!/bin/bash
# Sabah AI bot — bitta buyruq bilan o'rnatish / yangilash (macOS).
#   bash <(curl -fsSL https://raw.githubusercontent.com/ZOKIRJON0063003/vapi-app/main/bot/install.sh)
# Bot fonda doimiy ishlaydi: terminal yopilsa ham, Mac qayta yonganda ham o'zi ishga tushadi.
set -e

DIR="$HOME/vapi-bot"
RAW="https://raw.githubusercontent.com/ZOKIRJON0063003/vapi-app/main/bot"
LABEL="com.sabah.aibot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }
ask() { local v; read -r -p "$1" v </dev/tty; printf "%s" "$v"; }
ask_secret() { local v; read -r -s -p "$1" v </dev/tty; echo >/dev/tty; printf "%s" "$v"; }
setenv() {  # setenv KEY VALUE  — .env dagi qatorni almashtiradi yoki qo'shadi
  touch "$DIR/.env"; chmod 600 "$DIR/.env"
  grep -v "^$1=" "$DIR/.env" > "$DIR/.env.tmp" || true
  printf "%s=%s\n" "$1" "$2" >> "$DIR/.env.tmp"
  mv "$DIR/.env.tmp" "$DIR/.env"; chmod 600 "$DIR/.env"
}
getenv() { grep -E "^$1=" "$DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2-; }

say "1/5  Papka va Python"
mkdir -p "$DIR"; cd "$DIR"
PY=$(command -v python3 || true)
[ -z "$PY" ] && { echo "❌ python3 topilmadi. Avval: brew install python"; exit 1; }
[ -x venv/bin/python ] || "$PY" -m venv venv
venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1 || true
venv/bin/python -m pip install -q "python-telegram-bot>=22" httpx anthropic
echo "✅ Kutubxonalar o'rnatildi"

say "2/5  Bot fayllari (eng yangi versiya)"
curl -fsSL -o bot.py  "$RAW/bot.py"
curl -fsSL -o tune.py "$RAW/tune.py"
echo "✅ bot.py va tune.py yuklandi"

say "3/5  Kalitlar"
TOK=$(getenv BOT_TOKEN)
until [[ "$TOK" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{30,}$ ]] && \
      curl -fsS "https://api.telegram.org/bot$TOK/getMe" 2>/dev/null | grep -q '"ok":true'; do
  [ -n "$TOK" ] && echo "❌ Telegram token ishlamayapti."
  TOK=$(ask_secret "Telegram BOT_TOKEN (BotFather'dan, yozilgani ko'rinmaydi): ")
done
setenv BOT_TOKEN "$TOK"
echo "✅ Telegram token ishlayapti"

VK=$(getenv VAPI_API_KEY)
until [ -n "$VK" ] && curl -fsS -H "Authorization: Bearer $VK" https://api.vapi.ai/assistant >/dev/null 2>&1; do
  [ -n "$VK" ] && echo "❌ Vapi kaliti ishlamayapti."
  VK=$(ask_secret "Vapi PRIVATE key (dashboard.vapi.ai → API Keys → Private, ko'rinmaydi): ")
done
setenv VAPI_API_KEY "$VK"
echo "✅ Vapi kaliti ishlayapti"
grep -q '^ADMIN_IDS=' .env || setenv ADMIN_IDS 449766496

say "4/5  Ovozli AI ni sozlash (o'zbekcha eshitish, tez model)"
# Assistant ID ni bot o'zi aniqlaydi; tune.py ga kerak bo'lsa, botni bir marta qisqa ishga tushiramiz
if [ -z "$(getenv VAPI_ASSISTANT_ID)" ]; then
  venv/bin/python - <<'PY' || true
import os, re, httpx
env = dict(re.findall(r"(?m)^([A-Z_]+)=(.*)$", open(".env").read()))
r = httpx.get("https://api.vapi.ai/assistant", headers={"Authorization": f"Bearer {env['VAPI_API_KEY']}"}, timeout=20)
lst = r.json() if r.status_code == 200 else []
if lst:
    open(".env", "a").write(f"VAPI_ASSISTANT_ID={lst[0]['id']}\n")
PY
fi
venv/bin/python tune.py || echo "⚠️  Ovoz sozlamasi o'tkazib yuborildi (keyinroq: python tune.py)"

say "5/5  Botni fonda doimiy ishga tushirish"
# eski nusxalarni to'xtatamiz (hikcam_bot ga tegmaymiz)
launchctl unload "$PLIST" 2>/dev/null || true
pgrep -f "$DIR/bot.py" | xargs kill 2>/dev/null || true
pgrep -fl "bot.py" | grep -v hikcam | grep -iE "python[0-9.]* bot.py$" | awk '{print $1}' | xargs kill 2>/dev/null || true
sleep 2

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$DIR/venv/bin/python</string><string>$DIR/bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$DIR/bot.log</string>
  <key>StandardErrorPath</key><string>$DIR/bot.log</string>
</dict>
</plist>
PL
: > bot.log
launchctl load "$PLIST"

echo "Bot ishga tushmoqda…"
for i in $(seq 1 30); do
  grep -q "Bot ishlayapti" bot.log 2>/dev/null && break
  grep -q "❌" bot.log 2>/dev/null && break
  sleep 1
done
echo; grep -E "^(✅|ℹ️|⚠️|❌|🎉)" bot.log || tail -20 bot.log

if grep -q "Bot ishlayapti" bot.log; then
  say "🎉 TAYYOR! Bot fonda ishlayapti."
  cat <<TXT
  • Terminalni yopsangiz ham bot ishlayveradi. Mac qayta yonsa — o'zi ishga tushadi.
  • Mac uxlab qolmasligi uchun qopqog'ini yopmang (yoki quvvatga ulab qo'ying).
  • Endi hammasi Telegram'da: /admin, /faq, /kalit, /assistant, /log

  Yangilash yoki qayta o'rnatish — shu buyruqning o'zi:
    bash <(curl -fsSL $RAW/install.sh)
  Botni to'xtatish:
    launchctl unload ~/Library/LaunchAgents/$LABEL.plist
TXT
else
  say "⚠️  Bot ishga tushmadi. Yuqoridagi xabarni Claude'ga yuboring."
fi
