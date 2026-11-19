"""토요일 14시(한국·일본 시각) 한국·일본·미국 주간 관심사 → 일본 뉴스+커뮤니티 채널(2026-11-19 요청)

커뮤니티봇 이어 달리기(community.yml)에서 매시 community.py 다음에 실행.
- 매 실행: 나라별 아이폰 무료 앱 순위를 하루 한 번 저장(다음 주 '급상승 앱' 비교 기준)
- 토요일 13:45 이후 첫 실행: 자료 수집 → Gemini로 한국어 번역·요약 → 14:00까지 기다렸다 채널에 발송(주 1회)
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY, 선택 YOUTUBE_API_KEY, TRENDS_NOW=1(지금 바로), DRY_RUN=1
"""
import json, os, re, sys, time
from datetime import datetime
import requests
import bot
import trends_weekly as TW

STATE_PATH = "community_state.json"
SCHEMA = {"type": "OBJECT", "properties": {
    "summary": {"type": "ARRAY", "items": {"type": "STRING"}},
    "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"id": {"type": "STRING"}, "ko": {"type": "STRING"}}, "required": ["id", "ko"]}}},
    "required": ["summary", "items"]}

def log(*a): print("[trends]", *a, file=sys.stderr, flush=True)

def gem(state):
    def call(prompt):
        key = os.getenv("GEMINI_API_KEY", "")
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 12000, "responseMimeType": "application/json", "responseSchema": SCHEMA}}
        for m in bot._order(state, bot.WRITE_MODELS + bot.TRIAGE_MODELS, bot.available_models(state, key)):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}", json=body, timeout=180)
                if r.status_code != 200: log(m, r.status_code); continue
                cand = (r.json().get("candidates") or [{}])[0]
                txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
                out = json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip())
                if isinstance(out, dict) and out.get("items"): return out
            except Exception as e:
                log(m, str(e)[:100])
        return {}
    return call

def send(text):
    if os.getenv("DRY_RUN") == "1": print(text); return True
    tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) > 3800: parts.append(buf); buf = ""
        buf += line + "\n"
    parts.append(buf)
    ok = True
    for p in parts:
        for _ in range(3):
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": p, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
            if r.status_code == 429: time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1); continue
            ok = ok and r.status_code == 200
            if r.status_code != 200: log("telegram", r.status_code, r.text[:150])
            break
        time.sleep(1.2)
    return ok

def main():
    try: s = json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception: s = {}
    s.setdefault("model", {"names": [], "checked": 0})
    ts = s.setdefault("trends", {})
    try: TW.snap_apps(ts)
    except Exception as e: log("app snapshot", e)
    now = datetime.now(bot.JST); wk = now.strftime("%G-%V")
    due = now.weekday() == 5 and (now.hour, now.minute) >= (13, 45) and ts.get("done") != wk
    if due or os.getenv("TRENDS_NOW") == "1":
        from datetime import timedelta
        lab = f"{(now - timedelta(days=7)):%m/%d}~{(now - timedelta(days=1)):%m/%d}"
        data = TW.collect(ts, log=log)
        msgs = [TW.render(geo, data[geo], TW.ai_notes(data[geo], gem(s)), lab) for geo, *_ in TW.GEOS]
        t14 = now.replace(hour=14, minute=0, second=0, microsecond=0)
        wait = (t14 - datetime.now(bot.JST)).total_seconds()
        if 0 < wait < 1200 and os.getenv("TRENDS_NOW") != "1": log(f"14:00까지 {wait:.0f}초 대기"); time.sleep(wait)
        if all(send(m) for m in msgs) and os.getenv("TRENDS_NOW") != "1": ts["done"] = wk
        log("발송", len(msgs))
    json.dump(s, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False)

if __name__ == "__main__":
    main()
