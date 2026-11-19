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
def google_week(geo, n=30):   # 부고·스포츠를 빼고도 15개 남게 넉넉히
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

# ───────── 유튜브 검색 급상승(구글 트렌드 'YouTube 검색', 2026-11-19) ─────────
YT_JS = """async (geo) => {
  const req = {comparisonItem: [{keyword: '', geo: geo, time: 'now 7-d'}], category: 0, property: 'youtube'};
  const r = await fetch('/trends/api/explore?hl=ko&tz=-540&req=' + encodeURIComponent(JSON.stringify(req)));
  const tx = await r.text(); if (!r.ok) return 'HTTP ' + r.status;
  const w = JSON.parse(tx.slice(tx.indexOf('{'))).widgets.find(x => x.id === 'RELATED_QUERIES');
  const r2 = await fetch('/trends/api/widgetdata/relatedsearches?hl=ko&tz=-540&req=' + encodeURIComponent(JSON.stringify(w.request)) + '&token=' + w.token);
  const t2 = await r2.text(); if (!r2.ok) return 'HTTP ' + r2.status;
  const L = JSON.parse(t2.slice(t2.indexOf('{'))).default.rankedList;
  return (L[1] || {rankedKeyword: []}).rankedKeyword.map(k => [k.query, k.formattedValue]);
}"""

def youtube_search(geo, n=12):
    """지난 7일 유튜브 검색 급상승어 [(검색어, 상승률)] — 비슷한 말(gta 6 / gta 6 review)은 하나로"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try: b = p.chromium.launch(channel="chrome")
        except Exception: b = p.chromium.launch()
        try:
            pg = b.new_page(locale="ko-KR")
            pg.goto(f"https://trends.google.com/trends/explore?date=now%207-d&geo={geo}&gprop=youtube&hl=ko", timeout=60000)
            pg.wait_for_timeout(4000)
            rows = pg.evaluate(YT_JS, geo)
        finally:
            b.close()
    if not isinstance(rows, list): raise RuntimeError(str(rows))
    def same(a, b):   # 포함 관계, 앞 4글자 이상 같음, 숫자 아닌 낱말(2글자+) 공유 → 같은 주제
        ka, kb = re.sub(r"[\s\W_]+", "", a.lower()), re.sub(r"[\s\W_]+", "", b.lower())
        if ka in kb or kb in ka or (len(ka) >= 4 and len(kb) >= 4 and ka[:4] == kb[:4]): return True
        ta = {t for t in a.lower().split() if len(t) >= 2 and not t.isdigit()}
        return bool(ta & {t for t in b.lower().split() if len(t) >= 2 and not t.isdigit()})
    out = []
    for q, v in rows:
        if not q.strip() or any(same(q, o[0]) for o in out): continue
        out.append((q, "급등" if v in ("급등", "Breakout") else v))
        if len(out) >= n: break
    return out

# ───────── 넷플릭스 나라별 주간 TOP10(공식 공개 자료, 2026-11-19) ─────────
NF_URL = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"

def netflix_top():
    """{나라: [(분류, 순위, 제목, 시즌, 누적 주수)]} — 가장 최근 주, 시리즈 5·영화 3"""
    import csv, io
    raw = _get(NF_URL, timeout=90, headers={"User-Agent": "Mozilla/5.0"}).decode("utf-8", "ignore")
    rows = [r for r in csv.DictReader(io.StringIO(raw), delimiter="\t") if r.get("country_iso2") in ("KR", "JP", "US")]
    last = max(r["week"] for r in rows)
    out = {}
    for r in rows:
        if r["week"] != last: continue
        cat = "시리즈" if r["category"] == "TV" else "영화"
        if int(r["weekly_rank"]) > (5 if cat == "시리즈" else 3): continue
        sea = r["season_title"] if r["season_title"] not in ("N/A", "", r["show_title"]) else ""
        out.setdefault(r["country_iso2"], []).append((cat, int(r["weekly_rank"]), r["show_title"], sea, int(r.get("cumulative_weeks_in_top_10") or 0)))
    for v in out.values(): v.sort(key=lambda x: (x[0] != "시리즈", x[1]))
    return out, last

# ───────── 일본 X 트렌드·note 급상승(커뮤니티 봇이 매시 모은 기록, 이미 한국어) ─────────
def _nfweek(w):
    """넷플릭스 주(일요일 끝) '2026-11-15' → '11/09~11/15'"""
    try: e = dt.date.fromisoformat(w); return f"{e - dt.timedelta(days=6):%m/%d}~{e:%m/%d}"
    except Exception: return w or ""

def jp_topics(recs, n=8, days=7):
    """[{ts, ko, cat, reason}] → 지난 7일 자주 뜬 순 [(주제, 분류, 이유, 횟수)]. 스포츠·부고 제외"""
    cut, cnt, last = time.time() - days * 86400, {}, {}
    for r in recs or []:
        if r.get("ts", 0) < cut or not r.get("ko"): continue
        if "스포츠" in (r.get("cat") or "") or re.search(r"사망|별세|부고|타계|추모", r.get("reason") or ""): continue
        k = re.sub(r"\s+", "", r["ko"])
        cnt[k] = cnt.get(k, 0) + 1; last[k] = r
    top = sorted(cnt, key=lambda k: -cnt[k])[:n]
    return [(last[k]["ko"], last[k].get("cat") or "", last[k].get("reason") or "", cnt[k]) for k in top]

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

def wiki_rising(lang, end, n=16):
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

# ───────── 왜 떴는지: 구글 뉴스 제목 ─────────
def news_heads(q, lang, geo, n=3):
    """검색어로 지난 7일 구글 뉴스 제목 n개(언론사 이름은 뗌) — Gemini가 '왜 떴는지'를 추측 대신 기사 근거로 쓰게(2026-11-19)"""
    import html
    hl = {"ko": "ko", "ja": "ja", "en": "en-US"}[lang]
    u = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": f"{q} when:7d", "hl": hl, "gl": geo, "ceid": f"{geo}:{hl.split('-')[0]}"})
    try: x = _get(u, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).decode("utf-8", "ignore")
    except Exception: return []
    return [re.sub(r"\s+-\s+[^-]+$", "", html.unescape(t)).strip()[:90] for t in re.findall(r"<item><title>(.*?)</title>", x)[:n]]

def attach_news(d, geo):
    for key in ("gweek", "ysearch", "wiki"):
        heads = []
        for it in d.get(key) or []:
            heads.append(news_heads(it[0], d["lang"], geo)); time.sleep(0.4)
        d[key + "_news"] = heads

# ───────── 묶기 ─────────
def collect(state, log=print):
    """나라별 원자료. state: {'apps': {날짜: {cc: [[id, name], ...]}}} — 앱 기준 순위용(매일 쌓음)"""
    today = dt.date.today()
    end = today - dt.timedelta(days=1)
    data = {}
    for geo, ko, lang, cc in GEOS:
        d = {"name": ko, "lang": lang}
        for k, fn in (("gweek", lambda: google_week(geo)), ("ysearch", lambda: youtube_search(geo)), ("wiki", lambda: wiki_rising(lang, end))):
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
        try: attach_news(d, geo)
        except Exception as e: log("trends", geo, "news error", str(e)[:120])
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
- 모든 항목을 한국어로: 각 id마다 "한국어 이름 — 왜 떴는지 한 줄(35자 안팎)". 예: "나카타 노아 — 배우, 23세로 급서 소식".
- '왜 떴는지'는 함께 준 [뉴스] 기사 제목과 관련 검색어를 근거로 쓴다. 근거가 없으면 지어내지 말고 무엇인지(인물·스포츠·드라마 등)만.
- cat(분류): "부고"(사망·추모), "스포츠"(경기·선수·팀·대회·부상·이적), "연예", "정치·사회", "사건사고", "소비·제품", "기술·게임", "금융·투자", "기타" 중 하나. 운동선수의 사망은 "부고".
- stocks(관련 상장사): cat이 소비·제품/기술·게임/금융·투자/연예일 때만, 이 관심이 매출로 이어질 상장사 0~2개. 일본 상장사를 우선하고 없으면 해외. 직접 관련이 확실할 때만 쓰고, 애매하면 빈 배열.
  각 상장사: ko(한국어 회사명), ticker(일본은 4자리 코드, 한국은 6자리, 미국은 티커), ex(JP/KS/KQ/US/기타 거래소 코드), local(현지 정식 회사명 — 일본은 일본어 정식 사명, 예: "カプコン")
- 고유명사는 한국에서 통용되는 표기나 한글 발음. 일본어·영어 원문을 그대로 남기지 말 것.
- summary: 부고·스포츠는 빼고 이번 주 관심사를 3줄로(정치·사회, 연예, 소비·기술·투자처럼 묶어서). 투자와 이어지는 신호(기업·제품·소비 트렌드)가 있으면 꼭 한 줄 넣기.
출력 JSON: {{"summary": ["...", "...", "..."], "items": [{{"id": "gw0", "ko": "한국어 이름 — 설명", "cat": "기술·게임", "stocks": [{{"ko": "캡콤", "ticker": "9697", "ex": "JP", "local": "カプコン"}}]}}, ...]}}

{rows}"""

def ai_notes(d, gemini_json):
    rows = []
    nw = lambda key, k: " [뉴스] " + " / ".join((d.get(key + "_news") or [[]] * 99)[k]) if k < len(d.get(key + "_news") or []) and d[key + "_news"][k] else ""
    for k, it in enumerate(d.get("gweek") or []): rows.append(f"gw{k}: {it[0]} (관련: {it[3]})" + nw("gweek", k))
    for k, it in enumerate(d.get("ysearch") or []): rows.append(f"ys{k}: {it[0]} (유튜브 검색어)" + nw("ysearch", k))
    for k, it in enumerate(d.get("apps") or []): rows.append(f"ap{k}: {it[0]}")
    for k, it in enumerate(d.get("netflix") or []): rows.append(f"nf{k}: {it[2]}" + (f" / {it[3]}" if it[3] else "") + f" (넷플릭스 {it[0]}, 한국어 공식 제목이 있으면 그것으로)")
    for k, it in enumerate(d.get("wiki") or []): rows.append(f"wk{k}: {it[0]}" + nw("wiki", k))
    for k, it in enumerate(d.get("yt") or []): rows.append(f"yt{k}: {it[0]} / 채널 {it[1]}")
    if not rows: return {}
    out = gemini_json(PROMPT.format(name=d["name"], lang=d["lang"], rows="\n".join(rows)))
    if not isinstance(out, dict): return {}
    if isinstance(out.get("items"), list):   # [{"id","ko"}] → {id: ko}
        out["cats"] = {x.get("id"): x.get("cat") for x in out["items"] if isinstance(x, dict)}
        out["stocks"] = {x.get("id"): check_stocks(x.get("stocks")) for x in out["items"] if isinstance(x, dict) and x.get("cat") in INVEST}
        out["items"] = {x.get("id"): x.get("ko") for x in out["items"] if isinstance(x, dict)}
    return out

def esc(s): return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

DROP = {"부고", "스포츠"}   # 사용자 요청(2026-11-19): 부고·스포츠는 빼기
# ───────── 관련주 티커 확인(2026-11-19) ─────────
_LISTS = {}
def _norm(t):
    import unicodedata
    t = unicodedata.normalize("NFKC", t or "").lower()
    for w in ("株式会社", "(株)", "ホールディングス", "holdings", "グループ", "㈱", "주식회사", "(주)", "홀딩스", "지주"):
        t = t.replace(w, "")
    return re.sub(r"[\s・.,&'\-()（）]", "", t)

def _listed(kind):
    """data/jpx_list.csv(일본 상장사, 매월 갱신)·krx_list.csv → {코드: 이름}"""
    if kind not in _LISTS:
        import csv
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"{kind}_list.csv")
        try: _LISTS[kind] = {r["code"].strip().upper(): r["name"] for r in csv.DictReader(open(f, encoding="utf-8"))}
        except Exception: _LISTS[kind] = {}
    return _LISTS[kind]

def _same(a, b):
    import difflib
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6)

def check_stocks(sts):
    """AI가 고른 관련주 → 일본·한국은 상장사 목록에서 코드가 있고 회사명이 맞을 때만 남김, 해외는 그대로(미확인). 표시용 문자열."""
    out = []
    for x in sts if isinstance(sts, list) else []:
        if not isinstance(x, dict): continue
        ko_nm, tk, ex = (x.get("ko") or "").strip(), (x.get("ticker") or "").strip().upper(), (x.get("ex") or "").strip().upper()
        if not ko_nm or not tk: continue
        if ex == "JP":
            nm = _listed("jpx").get(tk)
            if not nm or not _same(x.get("local"), nm): continue   # 없는 코드거나 다른 회사 → 버림
        elif ex in ("KS", "KQ"):
            nm = _listed("krx").get(tk.zfill(6))
            if not nm or not (_same(ko_nm, nm) or _same(x.get("local"), nm)): continue
            tk = tk.zfill(6)
        out.append(f"{ko_nm}({tk} {ex or '?'})")
    return ", ".join(out[:2])

INVEST = {"소비·제품", "기술·게임", "금융·투자", "연예"}   # 관련 상장사를 붙이는 분류(2026-11-19)
ORDER = ["소비·제품", "기술·게임", "금융·투자", "연예", "정치·사회", "사건사고", "기타"]

# ───────── 주 단위 연속성(2026-11-19): 새로 등장 / n주째 ─────────
def _key(t): return re.sub(r"[\s\W_]+", "", (t or "").lower())

def _prev_week(wk, n=1):
    y, w = map(int, wk.split("-"))
    d = dt.date.fromisocalendar(y, w, 6) - dt.timedelta(weeks=n)
    return f"{d.isocalendar()[0]}-{d.isocalendar()[1]:02d}"

def mark_streaks(data, hist, wk):
    """hist: {주: {나라: {"gw": [키...], "wk": [...]}}}. 지난주 기록이 있을 때만 표시(첫 주는 표시 안 함)"""
    for geo, *_ in GEOS:
        d = data[geo]; d["streak"] = {}
        if geo not in (hist.get(_prev_week(wk)) or {}): continue
        for key, src in (("gw", "gweek"), ("ys", "ysearch"), ("wk", "wiki")):
            for k, x in enumerate(d.get(src) or []):
                n, w = 1, _prev_week(wk)
                while _key(x[0]) in ((hist.get(w) or {}).get(geo) or {}).get(key, []):
                    n += 1; w = _prev_week(w)
                d["streak"][f"{key}{k}"] = n

def record_week(data, hist, wk):
    hist[wk] = {geo: {"gw": [_key(x[0]) for x in data[geo].get("gweek") or []], "ys": [_key(x[0]) for x in data[geo].get("ysearch") or []],
                      "wk": [_key(x[0]) for x in data[geo].get("wiki") or []]}
                for geo, *_ in GEOS if data[geo].get("gweek") or data[geo].get("wiki")}
    for w in sorted(hist)[:-10]: hist.pop(w, None)

# ───────── 세 나라 공통 관심사(맨 앞 메시지) ─────────
CROSS_PROMPT = """아래는 지난 1주 한국·일본·미국에서 새로 뜬 관심사(이미 한국어로 정리, [분류] 표시)다.
1) common: 두 나라 이상에서 같이 뜬 주제(같은 게임·제품·기업·사건·인물·현상). 이름이 달라도 같은 주제면 묶기. 억지로 만들지 말고 없으면 빈 배열.
   각 항목: theme(주제 한국어), where(예: "일본·미국"), why(왜 같이 떴는지 한 줄, 아래 목록 근거로만).
2) diff: 나라별 이번 주 특징 한 줄씩(한국, 일본, 미국 순서로 3개). 소비·기술·투자와 이어지는 것을 우선.
출력 JSON: {{"common": [{{"theme": "...", "where": "...", "why": "..."}}], "diff": ["한국: ...", "일본: ...", "미국: ..."]}}

{rows}"""

def cross_rows(data, notes):
    rows = []
    for geo, ko_name, *_ in GEOS:
        it = (notes.get(geo) or {}).get("items") or {}; cats = (notes.get(geo) or {}).get("cats") or {}
        for key, src in (("gw", "gweek"), ("ys", "ysearch"), ("ap", "apps"), ("nf", "netflix"), ("wk", "wiki"), ("yt", "yt")):
            for k, x in enumerate(data[geo].get(src) or []):
                if cats.get(f"{key}{k}") in DROP: continue
                rows.append(f"{ko_name} [{cats.get(f'{key}{k}') or '-'}] {it.get(f'{key}{k}') or (x[2] if key == 'nf' else x[0])}" + (" (넷플릭스)" if key == "nf" else ""))
        for t in data[geo].get("jptopics") or []:
            rows.append(f"{ko_name} [{t[1]}] {t[0]} — {t[2]} (X·note)")
    return rows

def render_cross(out, week_lab):
    L = [f"🌐 <b>이번 주 세 나라 관심사 비교</b> · {week_lab}", "", "<b>두 나라 이상 공통</b>"]
    com = [c for c in (out or {}).get("common") or [] if isinstance(c, dict) and c.get("theme")]
    L += [f"• <b>{esc(c['theme'])}</b> ({esc(c.get('where', ''))}) — {esc(c.get('why', ''))}" for c in com[:6]] or ["• 이번 주는 겹치는 주제 없음"]
    diff = [x for x in (out or {}).get("diff") or [] if isinstance(x, str)]
    if diff: L += ["", "<b>나라별 특징</b>"] + [f"• {esc(x)}" for x in diff[:3]]
    L += ["", "<i>나라별 상세는 아래 메시지 3개. 부고·스포츠 제외</i>"]
    return "\n".join(L)

def render(geo, d, notes, week_lab):
    it = (notes or {}).get("items") or {}
    cats = (notes or {}).get("cats") or {}
    keep = lambda key, rows, n: [(k, x) for k, x in enumerate(rows or []) if cats.get(f"{key}{k}") not in DROP][:n]
    dropped = sum(1 for v in cats.values() if v in DROP)
    stk = (notes or {}).get("stocks") or {}
    stq = d.get("streak") or {}
    ko = lambda key, raw: esc(it.get(key) or raw) + (" 🆕" if stq.get(key) == 1 else f" <b>({stq[key]}주째)</b>" if stq.get(key, 0) >= 2 else "")
    sk = lambda key: f"\n   <i>→ 관련주(추정): {esc(stk[key])}</i>" if stk.get(key) else ""
    flag = {"KR": "🇰🇷", "JP": "🇯🇵", "US": "🇺🇸"}[geo]
    L = [f"{flag} <b>요즘 {d['name']} 관심사</b> · {week_lab}"]
    sm = (notes or {}).get("summary") or []
    if sm: L += ["", "<b>요약</b>"] + [f"• {esc(x)}" for x in sm[:3]]
    if d.get("gweek"):
        L += ["", "<b>① 구글 급상승 · 지난 7일</b> " + ("(성격별, 묶음 안은 검색량 순)" if cats else "(검색량 순)")]
        rows = keep("gw", d["gweek"], 15)
        if cats:   # 성격별로 묶기(소비·기술·금융을 위로), 묶음 안은 검색량 순(2026-11-19)
            for c in ORDER:
                grp = [(k, x) for k, x in rows if (cats.get(f"gw{k}") if cats.get(f"gw{k}") in ORDER else "기타") == c]
                if grp: L += [f"<u>{c}</u>"] + [f"• {ko(f'gw{k}', x[0])} · {x[1]}" + sk(f"gw{k}") for k, x in grp]
        else:
            L += [f"{j + 1}. {ko(f'gw{k}', x[0])} · {x[1]}" + sk(f"gw{k}") for j, (k, x) in enumerate(rows)]
    if d.get("ysearch"):
        L += ["", "<b>② 유튜브 검색 급상승 · 지난 7일</b> (상승률)"]
        L += [f"• {ko(f'ys{k}', x[0])} · {x[1]}" + sk(f"ys{k}") for k, x in keep("ys", d["ysearch"], 10)]
    if d.get("apps") is not None:
        L += ["", f"<b>③ 앱스토어 무료 앱 급상승</b> ({d.get('apps_base') or '이전 기록'} 대비, 아이폰)"]
        L += [f"• {ko(f'ap{k}', x[0])} · " + (f"{x[1]}위 → {x[2]}위" if x[1] else f"새로 진입 {x[2]}위") + sk(f"ap{k}") for k, x in enumerate(d["apps"])] or ["• 큰 변화 없음"]
    if d.get("netflix"):
        L += ["", "<b>④ 넷플릭스 주간 TOP</b> (" + _nfweek(d.get("netflix_week")) + ", 공식)"]
        L += [f"• {x[0]} {x[1]}위 {esc(it.get(f'nf{k}') or x[2])}" + (" 🆕" if x[4] == 1 else f" ({x[4]}주째)") for k, x in enumerate(d["netflix"])]
    if d.get("jptopics"):
        L += ["", "<b>+ 일본 X 트렌드·note 화제</b> (지난 7일 자주 뜬 순, 커뮤니티 봇 집계)"]
        L += [f"• {esc(t[0])} <i>[{esc(t[1])}]</i> — {esc(t[2][:60])}" for t in d["jptopics"]]
    if d.get("wiki"):
        L += ["", "<b>⑤ 위키백과 급상승 문서</b> (지난 7일 조회수)"]
        L += [f"• {ko(f'wk{k}', x[0])} · {x[1] / 1e4:,.1f}만 회" + (f"(전주 {x[2]:.1f}배)" if x[2] else "(전주 상위권 밖)") + sk(f"wk{k}") for k, x in keep("wk", d["wiki"], 10)]
    if d.get("yt"):
        L += ["", "<b>⑥ 유튜브 인기 동영상</b>"]
        L += [f"• {ko(f'yt{k}', x[0])} · 조회 {x[2] / 1e4:,.0f}만" + sk(f"yt{k}") for k, x in keep("yt", d["yt"], 8)]
    elif d.get("yt_err"):
        L += ["", "<i>⑥ 유튜브 인기 동영상: API 키에 YouTube Data API 권한이 없어 건너뜀</i>"]
    miss = [lab for k, lab in (("gweek", "구글 트렌드"), ("ysearch", "유튜브 검색어"), ("wiki", "위키백과")) if d.get(k) is None]
    if miss: L += ["", f"<i>못 가져온 항목: {', '.join(miss)}</i>"]
    if dropped: L += ["", f"<i>부고·스포츠 {dropped}건은 뺌</i>"]
    if stq: L += ["", "<i>🆕 = 이번 주 처음, (n주째) = n주 연속 급상승</i>"]
    if any(stk.values()): L += ["<i>관련주는 AI 추정(매출 연관성은 직접 확인). 일본·한국 티커는 상장사 목록과 대조해 틀린 것은 뺐고, 해외 티커는 미확인</i>"]
    L += ["", "<i>출처: 구글 트렌드(웹·유튜브 검색 급상승만, 상시 인기 검색어 제외)·구글 뉴스(이유 근거)·애플 앱스토어 순위·넷플릭스 TOP10·위키미디어 조회수·유튜브. 검색량은 구간값</i>"]
    return "\n".join(L)
