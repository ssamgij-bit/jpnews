"""토요일 14시: 한국·일본·미국 '요즘 사람들 관심사' 주간 정리(2026-11-19 요청) — 일본 뉴스+커뮤니티 채널로(trends.py)

- 구글 트렌드 급상승: 지난 7일(실시간 인기 화면, 브라우저로 읽음). 인기 검색어(날씨·유튜브 같은 상시 상위)는 빼고 '새로 뜬 것'만.
  월간은 사용자 요청으로 뺌(주간만)
- 앱스토어 무료 앱: 지난 기록 대비 새로 들어오거나 크게 오른 앱(애플 공개 순위 RSS)
- 위키백과: 지난 7일 조회수가 그 전 7일보다 크게 는 문서(위키미디어 조회수 API)
- 유튜브 인기 동영상(YouTube Data API, 키가 없거나 권한이 없으면 건너뜀)
- 일본어·영어는 Gemini로 한국어 번역 + 한 줄 설명
"""
import datetime as dt, json, os, re, time, urllib.parse, urllib.request

GEOS = [("KR", "한국", "ko", "kr"), ("JP", "일본", "ja", "jp"), ("US", "미국", "en", "us")]
UA = {"User-Agent": "jp-trends-bot/1.0 (weekly research digest)"}

def _get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()

def _vol(s):
    """'100만+'·'5천+'·'200K+'·'1M+' → 숫자"""
    s = (s or "").replace(",", "").replace("+", "").strip()
    m = re.match(r"([\d.]+)\s*(만|천|억|K|M|B)?", s, re.I)
    if not m: return 0
    v = float(m.group(1)); u = (m.group(2) or "").upper()
    return v * {"만": 1e4, "천": 1e3, "억": 1e8, "K": 1e3, "M": 1e6, "B": 1e9}.get(u, 1)

# ───────── 구글 트렌드 ─────────
def google_week(geo, n=15):
    """실시간 인기(지난 7일) 화면 → [(검색어, 검색량 문자열, 상승률, 관련 검색어)] 검색량 큰 순"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try: b = p.chromium.launch(channel="chrome")
        except Exception: b = p.chromium.launch()
        try:
            pg = b.new_page(locale="ko-KR")
            pg.goto(f"https://trends.google.com/trending?geo={geo}&hours=168&hl=ko", timeout=60000)
            pg.wait_for_selector("table tbody tr td", state="attached", timeout=40000); pg.wait_for_timeout(2500)
            rows = pg.eval_on_selector_all("table tbody tr", "rs=>rs.map(r=>[...r.querySelectorAll('td')].map(td=>td.innerText.replace(/\\s+/g,' ').trim()))")
        finally:
            b.close()
    out = []
    for r in rows:
        if len(r) < 5 or not r[1]: continue
        m = re.match(r"(\S+\+)\s*arrow_upward\s*([\d,]+%)", r[2])
        vol, pct = (m.group(1), m.group(2)) if m else (r[2].split(" ")[0], "")
        rel = re.sub(r"\s*외 \d+개$", "", r[4]).strip()
        out.append((r[1], vol, pct, rel[:80]))
    out.sort(key=lambda x: -_vol(x[1]))
    return out[:n]

# ───────── 앱스토어 ─────────
def app_top(cc, n=100):
    d = json.loads(_get(f"https://rss.marketingtools.apple.com/api/v2/{cc}/apps/top-free/{n}/apps.json"))
    return [(a["id"], a["name"]) for a in d["feed"]["results"]]

def app_rising(now_list, base_list, n=8):
    """기준 시점 대비 새로 들어오거나 20계단 넘게 오른 앱(지금 순위 50위 안) → [(이름, 전 순위 또는 None, 지금 순위)]"""
    base = {i: k + 1 for k, (i, _) in enumerate(base_list)}
    out = []
    for k, (i, nm) in enumerate(now_list[:50]):
        r = k + 1; b = base.get(i)
        if b is None or b - r >= 20: out.append((nm, b, r))
    return out[:n]

# ───────── 위키백과 ─────────
SKIP_WIKI = re.compile(r"^(특수|위키백과|Special|Wikipedia|特別|Wikipedia|ウィキペディア|Portal|포털|Help|도움말|File|파일)[:：]|^(대문|Main_Page|メインページ|-|Undefined)$", re.I)

def wiki_rising(lang, end, n=10):
    """end(포함)까지 7일 조회수 합 vs 그 전 7일 → 2배 넘게 는 문서 [(제목, 7일 조회수, 배수 또는 None)]"""
    def day(d):
        for k in range(4):   # 위키미디어는 요청이 몰리면 429 → 기다렸다 다시
            try: return json.loads(_get(f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{lang}.wikipedia/all-access/{d:%Y/%m/%d}"))["items"][0]["articles"]
            except Exception as e:
                if "429" not in str(e) or k == 3: return None
                time.sleep(4 * 2 ** k)
    def week(last):
        tot, ok = {}, 0
        for k in range(7):
            arts = day(last - dt.timedelta(days=k))
            if arts is None: continue
            ok += 1
            for a in arts:
                if SKIP_WIKI.search(a["article"]): continue
                tot[a["article"]] = tot.get(a["article"], 0) + a["views"]
            time.sleep(1)
        if ok < 5: raise RuntimeError(f"위키 조회수 {ok}/7일만 받음")   # 빈 날이 많으면 '급상승'이 엉터리가 되므로 생략
        return tot
    cur, prev = week(end), week(end - dt.timedelta(days=7))
    out = []
    for t, v in sorted(cur.items(), key=lambda x: -x[1]):
        p = prev.get(t)
        if p is None or v >= 2 * p:
            out.append((t.replace("_", " "), v, (v / p) if p else None))
        if len(out) >= n: break
    return out

# ───────── 유튜브 ─────────
def youtube_popular(geo, key, n=8):
    q = urllib.parse.urlencode({"part": "snippet,statistics", "chart": "mostPopular", "regionCode": geo, "maxResults": n, "key": key})
    d = json.loads(_get("https://www.googleapis.com/youtube/v3/videos?" + q))
    return [(v["snippet"]["title"], v["snippet"]["channelTitle"], int(v.get("statistics", {}).get("viewCount", 0))) for v in d.get("items", [])]

# ───────── 묶기 ─────────
def collect(state, log=print):
    """나라별 원자료. state: {'apps': {날짜: {cc: [[id, name], ...]}}} — 앱 기준 순위용(매일 쌓음)"""
    today = dt.date.today()
    end = today - dt.timedelta(days=1)
    data = {}
    for geo, ko, lang, cc in GEOS:
        d = {"name": ko, "lang": lang}
        for k, fn in (("gweek", lambda: google_week(geo)), ("wiki", lambda: wiki_rising(lang, end))):
            try: d[k] = fn()
            except Exception as e: d[k] = None; log("trends", geo, k, "error", str(e)[:120])
            time.sleep(2)
        try:
            now_list = app_top(cc)
            snaps = sorted((state.get("apps") or {}).items())
            base = next((v.get(cc) for day, v in snaps if day <= (today - dt.timedelta(days=6)).isoformat() and v.get(cc)), None) \
                or next((v.get(cc) for day, v in snaps if v.get(cc)), None)
            d["apps"] = app_rising(now_list, [tuple(x) for x in base]) if base else None
            d["apps_base"] = next((day for day, v in snaps if v.get(cc) and v.get(cc) == base), None) if base else None
            d["apps_top"] = [nm for _, nm in now_list[:5]]
        except Exception as e:
            d["apps"] = None; log("trends", geo, "apps error", str(e)[:120])
        key = os.environ.get("YOUTUBE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
        try: d["yt"] = youtube_popular(geo, key) if key else None
        except Exception as e: d["yt"] = None; d["yt_err"] = str(e)[:80]; log("trends", geo, "youtube error", str(e)[:120])
        data[geo] = d
    return data

def snap_apps(state):
    """매일 한 번 나라별 무료 앱 순위 저장(최근 14일만)"""
    day = dt.date.today().isoformat()
    apps = state.setdefault("apps", {})
    if day in apps: return
    snap = {}
    for _, _, _, cc in GEOS:
        try: snap[cc] = [list(x) for x in app_top(cc)]
        except Exception: pass
    if snap: apps[day] = snap
    for d in sorted(apps)[:-14]: apps.pop(d, None)

PROMPT = """너는 한국 애널리스트에게 '요즘 {name} 사람들이 무엇에 관심을 갖는지'를 정리해 주는 비서다.
아래는 지난 1주 동안 {name}에서 새로 뜬 검색어·앱·위키백과 문서·유튜브 영상 목록이다(원문 {lang}).
규칙
- 모든 항목을 한국어로: 각 id마다 "한국어 이름 — 왜 떴는지 한 줄(20자 안팎)". 관련 검색어·문맥으로 알 수 있는 것만 쓰고, 모르면 "— 설명 없음"이 아니라 무엇인지(인물·스포츠·드라마 등)만.
- 고유명사는 한국에서 통용되는 표기나 한글 발음. 일본어·영어 원문을 그대로 남기지 말 것.
- summary: 이번 주 관심사를 3줄로(정치·사건, 연예·스포츠, 소비·기술·투자처럼 묶어서). 투자와 이어지는 신호(기업·제품·소비 트렌드)가 있으면 꼭 한 줄 넣기.
출력 JSON: {{"summary": ["...", "...", "..."], "items": [{{"id": "gw0", "ko": "한국어 이름 — 설명"}}, ...]}}

{rows}"""

def ai_notes(d, gemini_json):
    rows = []
    for k, it in enumerate(d.get("gweek") or []): rows.append(f"gw{k}: {it[0]} (관련: {it[3]})")
    for k, it in enumerate(d.get("apps") or []): rows.append(f"ap{k}: {it[0]}")
    for k, it in enumerate(d.get("wiki") or []): rows.append(f"wk{k}: {it[0]}")
    for k, it in enumerate(d.get("yt") or []): rows.append(f"yt{k}: {it[0]} / 채널 {it[1]}")
    if not rows: return {}
    out = gemini_json(PROMPT.format(name=d["name"], lang=d["lang"], rows="\n".join(rows)))
    if not isinstance(out, dict): return {}
    if isinstance(out.get("items"), list):   # [{"id","ko"}] → {id: ko}
        out["items"] = {x.get("id"): x.get("ko") for x in out["items"] if isinstance(x, dict)}
    return out

def esc(s): return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def render(geo, d, notes, week_lab):
    it = (notes or {}).get("items") or {}
    ko = lambda key, raw: esc(it.get(key) or raw)
    flag = {"KR": "🇰🇷", "JP": "🇯🇵", "US": "🇺🇸"}[geo]
    L = [f"{flag} <b>요즘 {d['name']} 관심사</b> · {week_lab}"]
    sm = (notes or {}).get("summary") or []
    if sm: L += ["", "<b>요약</b>"] + [f"• {esc(x)}" for x in sm[:3]]
    if d.get("gweek"):
        L += ["", "<b>① 구글 급상승 · 지난 7일</b> (검색량 순)"]
        L += [f"{k + 1}. {ko(f'gw{k}', x[0])} · {x[1]}" for k, x in enumerate(d["gweek"])]
    if d.get("apps") is not None:
        L += ["", f"<b>② 앱스토어 무료 앱 급상승</b> ({d.get('apps_base') or '이전 기록'} 대비, 아이폰)"]
        L += [f"• {ko(f'ap{k}', x[0])} · " + (f"{x[1]}위 → {x[2]}위" if x[1] else f"새로 진입 {x[2]}위") for k, x in enumerate(d["apps"])] or ["• 큰 변화 없음"]
    if d.get("wiki"):
        L += ["", "<b>③ 위키백과 급상승 문서</b> (지난 7일 조회수)"]
        L += [f"• {ko(f'wk{k}', x[0])} · {x[1] / 1e4:,.1f}만 회" + (f"(전주 {x[2]:.1f}배)" if x[2] else "(전주 상위권 밖)") for k, x in enumerate(d["wiki"])]
    if d.get("yt"):
        L += ["", "<b>④ 유튜브 인기 급상승</b>"]
        L += [f"• {ko(f'yt{k}', x[0])} · 조회 {x[2] / 1e4:,.0f}만" for k, x in enumerate(d["yt"])]
    elif d.get("yt_err"):
        L += ["", "<i>④ 유튜브: API 키에 YouTube Data API 권한이 없어 건너뜀</i>"]
    miss = [lab for k, lab in (("gweek", "구글 트렌드"), ("wiki", "위키백과")) if d.get(k) is None]
    if miss: L += ["", f"<i>못 가져온 항목: {', '.join(miss)}</i>"]
    L += ["", "<i>출처: 구글 트렌드(급상승만, 상시 인기 검색어 제외)·애플 앱스토어 순위·위키미디어 조회수·유튜브. 검색량은 구간값</i>"]
    return "\n".join(L)
