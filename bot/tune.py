"""
Sabah AI assistantini tez va o'zbekchani yaxshi eshitadigan qilib sozlaydi.
Ishga tushirish:   cd ~/vapi-bot && source venv/bin/activate && python tune.py
Orqaga qaytarish:  python tune.py --undo
"""
import os
import re
import sys
import json
import glob
from datetime import datetime
import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
VAPI = "https://api.vapi.ai"
FIELDS = ["transcriber", "model", "voice", "startSpeakingPlan", "stopSpeakingPlan",
          "backgroundSpeechDenoisingPlan", "backgroundSound"]
FAST_OPENAI = {"gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1-nano", "gpt-5-nano"}


def env():
    d = {}
    for line in open(os.path.join(HERE, ".env"), encoding="utf-8", errors="ignore"):
        m = re.match(r"^\s*([A-Z_]+)\s*=\s*(.*)$", line)
        if m:
            d[m.group(1)] = m.group(2).strip()
    return d


def main():
    e = env()
    key, aid = e.get("VAPI_API_KEY"), e.get("VAPI_ASSISTANT_ID")
    if not key or not aid:
        sys.exit("❌ .env da VAPI_API_KEY yoki VAPI_ASSISTANT_ID yo'q. Avval: python bot.py")
    H = {"Authorization": f"Bearer {key}"}
    url = f"{VAPI}/assistant/{aid}"

    if "--undo" in sys.argv:
        files = sorted(glob.glob(os.path.join(HERE, "assistant_backup_*.json")))
        if not files:
            sys.exit("❌ Zaxira nusxa topilmadi.")
        old = json.load(open(files[-1]))
        patch = {k: old[k] for k in FIELDS if k in old}
        r = httpx.patch(url, headers=H, json=patch, timeout=30)
        sys.exit("✅ Eski sozlamalar qaytarildi." if r.status_code < 300 else f"❌ Xato {r.status_code}: {r.text[:400]}")

    r = httpx.get(url, headers=H, timeout=30)
    if r.status_code != 200:
        sys.exit(f"❌ Assistantni o'qib bo'lmadi ({r.status_code}): {r.text[:300]}")
    a = r.json()
    backup = os.path.join(HERE, f"assistant_backup_{datetime.now():%Y%m%d_%H%M%S}.json")
    json.dump(a, open(backup, "w"), ensure_ascii=False, indent=2)
    print(f"💾 Eski sozlamalar saqlandi: {os.path.basename(backup)}")

    tr, md, vc = a.get("transcriber") or {}, a.get("model") or {}, a.get("voice") or {}
    print(f"\nHozir:\n  🎧 Eshitish:  {tr.get('provider')} / {tr.get('model', '')} / til: {tr.get('language')}"
          f"\n  🧠 Model:     {md.get('provider')} / {md.get('model')}"
          f"\n  🔊 Ovoz:      {vc.get('provider')} / {vc.get('voiceId')}")

    model = dict(md)
    if model.get("provider") == "openai" and model.get("model") not in FAST_OPENAI:
        model["model"] = "gpt-4.1-mini"
    if not model.get("maxTokens"):
        model["maxTokens"] = 250  # qisqa javob = tezroq gapiradi
    for k in ("createdAt", "updatedAt"):
        model.pop(k, None)

    patch = {
        # O'zbek tilini yaxshi taniydigan eshitish
        "transcriber": {"provider": "azure", "language": "uz-UZ", "segmentationSilenceTimeoutMs": 500},
        # Tez o'zbekcha ovoz (Mini App'da ayol/erkak tanlansa, o'sha ishlatiladi)
        "voice": vc if (vc.get("provider") == "azure" and str(vc.get("voiceId", "")).startswith("uz-UZ"))
        else {"provider": "azure", "voiceId": "uz-UZ-MadinaNeural"},
        "model": model,
        # Gap tugashini tezroq aniqlash
        "startSpeakingPlan": {"waitSeconds": 0.4, "smartEndpointingEnabled": False},
        # Mijoz gapira boshlasa, AI darhol to'xtasin
        "stopSpeakingPlan": {"numWords": 0, "voiceSeconds": 0.2, "backoffSeconds": 1},
        # Atrofdagi shovqinni tozalash
        "backgroundSpeechDenoisingPlan": {"smartDenoisingPlan": {"enabled": True}},
        "backgroundSound": "off",
    }
    r = httpx.patch(url, headers=H, json=patch, timeout=30)
    if r.status_code >= 300:
        print(f"⚠️ To'liq sozlash qabul qilinmadi ({r.status_code}): {r.text[:300]}\n   Asosiylarini sozlab ko'ryapman…")
        basic = {k: patch[k] for k in ("transcriber", "voice", "startSpeakingPlan")}
        r = httpx.patch(url, headers=H, json=basic, timeout=30)
        if r.status_code >= 300:
            sys.exit(f"❌ Vapi qabul qilmadi ({r.status_code}): {r.text[:400]}")
    n = r.json()
    tr, md, vc = n.get("transcriber") or {}, n.get("model") or {}, n.get("voice") or {}
    print(f"\n✅ Yangi sozlamalar:\n  🎧 Eshitish:  {tr.get('provider')} / til: {tr.get('language')}"
          f"\n  🧠 Model:     {md.get('provider')} / {md.get('model')}"
          f"\n  🔊 Ovoz:      {vc.get('provider')} / {vc.get('voiceId')}"
          "\n\nEndi Telegram'da qayta sinab ko'ring. Yoqmasa:  python tune.py --undo")


if __name__ == "__main__":
    main()
