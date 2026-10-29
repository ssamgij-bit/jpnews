"""매일 아침 서울 오늘 날씨 → 텔레그램(커뮤니티 다이제스트와 같은 채널).

데이터: Open-Meteo(기상 예보, 대기질). API 키 필요 없음. Gemini를 쓰지 않아 한도와 무관하게 항상 나간다.
내용: 하늘 상태, 최저·최고(체감) 기온과 어제 대비, 출근길(08시)·퇴근길(18~19시) 기온과 비, 비·눈 확률과 예상 시간대,
      바람, 자외선, 미세먼지, 옷차림·우산 한 줄.
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. 선택: DRY_RUN=1, FORCE=1(시각·중복 확인 없이 바로 발송)
발송: 매일 06:30 KST. GitHub 예약 실행은 몇 시간씩 밀리거나 빠지므로 예약에 기대지 않고 '이어 달리기'로 돌린다:
      각 실행이 다음 발송 시각까지(최대 MAX_WAIT) 기다렸다가, 시각이 되면 보내고, 끝날 때 다음 실행을 직접 띄움.
      예약 실행은 이어 달리기가 끊겼을 때 다시 잇는 용도. 이미 보낸 날은 weather_state.json으로 건너뜀.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
LAT, LON = 37.5665, 126.9780  # 서울시청
DRY_RUN = os.getenv("DRY_RUN") == "1"

WMO = {0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "짙은 안개",
       51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비", 56: "어는 이슬비", 57: "어는 이슬비",
       61: "약한 비", 63: "비", 65: "강한 비", 66: "어는 비", 67: "어는 비",
       71: "약한 눈", 73: "눈", 75: "많은 눈", 77: "싸락눈", 80: "소나기", 81: "소나기", 82: "강한 소나기",
       85: "눈 소나기", 86: "강한 눈 소나기", 95: "뇌우", 96: "뇌우·우박", 99: "강한 뇌우·우박"}
WET = set(range(51, 68)) | set(range(80, 83)) | {95, 96, 99}
SNOW = set(range(71, 78)) | {85, 86}


def get(url, params):
    for wait in (0, 5, 15):
        time.sleep(wait)
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception as ex:
            print(f"[warn] {url}: {ex}", file=sys.stderr)
    raise RuntimeError(f"{url} 실패")


def pm_grade(pm10, pm25):
    """환경부 기준 등급(좋음/보통/나쁨/매우나쁨) 중 나쁜 쪽."""
    def g(v, cuts):
        return 0 if v <= cuts[0] else 1 if v <= cuts[1] else 2 if v <= cuts[2] else 3
    k = max(g(pm10, (30, 80, 150)), g(pm25, (15, 35, 75)))
    return ["좋음", "보통", "나쁨", "매우나쁨"][k]


def outfit(tmin, tmax):
    t = (tmin + tmax) / 2
    if t >= 27:
        return "반팔·반바지"
    if t >= 23:
        return "반팔·얇은 셔츠"
    if t >= 20:
        return "얇은 긴팔·가디건"
    if t >= 17:
        return "가디건·얇은 니트"
    if t >= 12:
        return "자켓·트렌치코트"
    if t >= 9:
        return "코트·니트"
    if t >= 5:
        return "두꺼운 코트·히트텍"
    return "패딩·목도리·장갑"


def spans(hours):
    """연속된 시각을 '09~12시'처럼 묶는다."""
    out, start, prev = [], None, None
    for h in hours:
        if start is None:
            start = prev = h
        elif h == prev + 1:
            prev = h
        else:
            out.append((start, prev))
            start = prev = h
    if start is not None:
        out.append((start, prev))
    return ", ".join(f"{a}시" if a == b else f"{a}~{b}시" for a, b in out)


def build():
    f = get("https://api.open-meteo.com/v1/forecast", {
        "latitude": LAT, "longitude": LON, "timezone": "Asia/Seoul", "past_days": 1, "forecast_days": 1,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,"
                 "precipitation_sum,precipitation_probability_max,wind_speed_10m_max,uv_index_max",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code"})
    d, h = f["daily"], f["hourly"]
    today = datetime.now(KST).strftime("%Y-%m-%d")
    i = d["time"].index(today)
    tmin, tmax = d["temperature_2m_min"][i], d["temperature_2m_max"][i]
    amin, amax = d["apparent_temperature_min"][i], d["apparent_temperature_max"][i]
    code, pop, rain = d["weather_code"][i], d["precipitation_probability_max"][i] or 0, d["precipitation_sum"][i] or 0
    wind, uv = (d["wind_speed_10m_max"][i] or 0) / 3.6, d["uv_index_max"][i] or 0  # km/h → m/s
    ytmin, ytmax = (d["temperature_2m_min"][i - 1], d["temperature_2m_max"][i - 1]) if i > 0 else (None, None)

    # 오늘 06~23시 중 비·눈 가능성(강수확률 40% 이상 또는 강수 0.1mm 이상) 시간대
    wet_hours, snow = [], False
    for t, p, mm, c in zip(h["time"], h["precipitation_probability"], h["precipitation"], h["weather_code"]):
        if t.startswith(today) and 6 <= int(t[11:13]) <= 23 and ((p or 0) >= 40 or (mm or 0) >= 0.1):
            wet_hours.append(int(t[11:13]))
            snow = snow or c in SNOW

    try:
        a = get("https://air-quality-api.open-meteo.com/v1/air-quality", {
            "latitude": LAT, "longitude": LON, "timezone": "Asia/Seoul", "forecast_days": 1, "hourly": "pm10,pm2_5"})
        pm10 = [v for t, v in zip(a["hourly"]["time"], a["hourly"]["pm10"]) if t.startswith(today) and v is not None]
        pm25 = [v for t, v in zip(a["hourly"]["time"], a["hourly"]["pm2_5"]) if t.startswith(today) and v is not None]
        dust = f"{pm_grade(max(pm10), max(pm25))} (미세 최대 {max(pm10):.0f} · 초미세 최대 {max(pm25):.0f}㎍/㎥)" if pm10 and pm25 else ""
    except Exception:
        dust = ""

    wd = "월화수목금토일"[datetime.now(KST).weekday()]
    lines = [f"<b>&gt;&gt;[서울 날씨] {today[5:]}({wd})</b> {WMO.get(code, '')}", ""]
    diff = ""
    if ytmax is not None:
        dmax, dmin = tmax - ytmax, tmin - ytmin
        diff = f" · 어제보다 최고 {dmax:+.0f}℃, 최저 {dmin:+.0f}℃"
    lines.append(f"• 기온: 최저 {tmin:.0f}℃ / 최고 {tmax:.0f}℃ (체감 {amin:.0f}~{amax:.0f}℃){diff}")
    lines.append("")
    commute_rain = []
    for label, hrs in (("출근길(08시)", (8,)), ("퇴근길(18~19시)", (18, 19))):
        idx = [k for k, t in enumerate(h["time"]) if t.startswith(today) and int(t[11:13]) in hrs]
        if not idx:
            continue
        temp = sum(h["temperature_2m"][k] for k in idx) / len(idx)
        feel = sum(h["apparent_temperature"][k] for k in idx) / len(idx)
        p = max(h["precipitation_probability"][k] or 0 for k in idx)
        mm = sum(h["precipitation"][k] or 0 for k in idx)
        sky = WMO.get(max(h["weather_code"][k] for k in idx), "")
        wet = p >= 40 or mm >= 0.1
        if wet:
            commute_rain.append(label[:3])
        lines.append(f"• {label}: {temp:.0f}℃ (체감 {feel:.0f}℃) · {sky} · 비 {p:.0f}%" + (" → 우산 필요" if wet else ""))
    lines.append("")
    if wet_hours or code in WET | SNOW:
        kind = "눈" if snow or code in SNOW else "비"
        when = spans(wet_hours) if wet_hours else "시간대 불확실"
        lines.append(f"• {kind}: 강수확률 최대 {pop:.0f}%, 예상 {when}" + (f", 하루 {rain:.1f}mm" if rain >= 0.1 else ""))
    else:
        lines.append(f"• 비: 소식 없음 (강수확률 최대 {pop:.0f}%)")
    lines.append("")
    lines.append(f"• 바람: 최대 {wind:.0f}m/s" + (" (강함)" if wind >= 9 else " (다소 강함)" if wind >= 6 else ""))
    lines.append("")
    lines.append(f"• 자외선: {uv:.0f}" + (" (매우 높음)" if uv >= 8 else " (높음)" if uv >= 6 else " (보통)" if uv >= 3 else " (낮음)"))
    if dust:
        lines.append("")
        lines.append(f"• 미세먼지: {dust}")
    tips = [f"옷차림은 {outfit(tmin, tmax)}"]
    if tmax - tmin >= 10:
        tips.append(f"일교차가 {tmax - tmin:.0f}℃로 커서 겉옷 챙기기")
    if commute_rain:
        tips.append(("출퇴근길" if len(commute_rain) == 2 else commute_rain[0]) + " 우산 챙기기")
    elif wet_hours or pop >= 40:
        tips.append("우산 챙기기")
    if dust.startswith(("나쁨", "매우나쁨")):
        tips.append("마스크 권장")
    if ytmin is not None and tmin - ytmin <= -5:
        tips.append("어제보다 크게 추워짐")
    lines.append("")
    lines.append("→ " + ", ".join(tips) + ".")
    lines.append("")
    lines.append("<i>Open-Meteo 예보(서울시청 기준).</i>")
    return "\n".join(lines)


def send(text):
    if DRY_RUN:
        print(text)
        return True
    tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=20)
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        print(f"[warn] telegram {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    return False


SEND_AT = (6, 30)   # KST 발송 시각
LATE_LIMIT = 9      # 이 시각(KST) 이후에야 발송 차례가 오면 보내지 않음(아침 정보가 무의미해짐)
MAX_WAIT = 5 * 3600 + 20 * 60  # 한 실행이 기다리는 최대 시간(GitHub 작업 한도 6시간 안쪽)
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_state.json")


def main():
    import json
    force = os.getenv("FORCE") == "1"
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {}
    now = datetime.now(KST)
    if not force:
        target = now.replace(hour=SEND_AT[0], minute=SEND_AT[1], second=0, microsecond=0)
        if st.get("sent") == now.strftime("%Y-%m-%d") or now.hour >= LATE_LIMIT:
            target += timedelta(days=1)  # 오늘 몫은 끝 → 내일 06:30
        wait = (target - now).total_seconds()
        if wait > MAX_WAIT:
            print(f"[info] 다음 발송 {target:%m-%d %H:%M}까지 {wait / 3600:.1f}시간 → {MAX_WAIT / 3600:.1f}시간 대기 후 다음 실행에 넘김")
            time.sleep(MAX_WAIT)
            return
        if wait > 0:
            print(f"[info] {wait / 60:.0f}분 대기 후 발송({target:%m-%d %H:%M})")
            time.sleep(wait)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if st.get("sent") == today and not force:
        print(f"[info] 오늘({today}) 이미 보냄")
        return
    if not send(build()):
        sys.exit(1)
    st["sent"] = today
    if not DRY_RUN:
        json.dump(st, open(STATE, "w"))


if __name__ == "__main__":
    main()
