"""일본 커뮤니티 화제글 → 한국어 요약(본문·댓글) → 텔레그램 채널 다이제스트.

소스: 하테나 북마크 인기 엔트리(종합), 걸즈채널 오늘의 인기 토픽, 토게터 주목 정리.
매시 실행(GitHub Actions)으로 화제도 스냅샷을 쌓고, 00·06·12·18시(JST=KST)에 다이제스트를 보낸다.
00·06시는 무음 발송. 상장사·증시 관련성 ★4 이상은 다이제스트를 기다리지 않고 즉시 보낸다.
상태는 community_state.json(워크플로우가 커밋). 뉴스봇(bot.py)의 Gemini·티커 대조 함수를 재사용한다.
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY
선택: TELEGRAM_ADMIN_CHAT_ID, COMMUNITY_THRESHOLD(기본 4), HATENA_MIN(100), GIRLS_MIN(300), TOGETTER_MIN(20000),
      PER_SOURCE(2), WATCH_SEARCH_N(6), WATCH_HATENA_MIN(10), WATCH_GIRLS_MIN(30),
      DIGEST_NOW=1(지금 바로 다이제스트), WEEKLY_NOW=1(지금 바로 주간 순위), DRY_RUN=1
관심 종목: watchlist.csv(티커, 추가 키워드). 일요일 18시 회차에 주간 기업·브랜드 언급 순위.
"""
import calendar
import csv
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta

import feedparser
import requests

import bot  # 같은 저장소의 뉴스봇: gemini(), company_line(), esc(), JST, UA 재사용
from bot import JST, UA, esc

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "community_state.json")
DRY_RUN = os.getenv("DRY_RUN") == "1"
THRESHOLD = int(os.getenv("COMMUNITY_THRESHOLD", "4"))
PER_SOURCE = int(os.getenv("PER_SOURCE", "2"))
SLOTS = [0, 6, 12, 18]           # 다이제스트 시각(JST)
SILENT_SLOTS = {0, 6}            # 무음 발송
LEAD_MIN = 15                    # 정각 15분 전 실행분부터 해당 회차로 간주(워크플로우는 매시 50분 실행)
WINDOW_H = 6.5                   # 화제도(증가량) 계산 구간
FALLBACK_AFTER_H = 2             # 요약이 이 시간 넘게 계속 실패하면 제목·링크만으로 발송
FAIL_ALERT_N = 3

SRC = {
    "hb": {"name": "하테나 북마크", "unit": "북마크", "min": int(os.getenv("HATENA_MIN", "100"))},
    "gc": {"name": "걸즈채널", "unit": "댓글", "min": int(os.getenv("GIRLS_MIN", "300"))},
    "tg": {"name": "토게터", "unit": "조회", "min": int(os.getenv("TOGETTER_MIN", "20000"))},
}
# 걸즈채널의 상시 잡담·실황 토픽(시리즈물)은 화제글로 보지 않는다
GC_SKIP = re.compile(r"[pP]art\s*\.?\s*\d+|【実況|実況・感想|集まれ|語ろう|語りたい|トピ$")


# ───────────────────────── 상태 ─────────────────────────
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("cands", {})       # key -> {src,title,url,extra,hist:[[ts,metric]],first,last,score,ko}
    s.setdefault("sent", {})        # key -> ts (14일 보관, 재발송 방지)
    s.setdefault("last_slot", "")
    s.setdefault("slot_fail_since", 0)
    s.setdefault("model", {"names": [], "checked": 0})
    s.setdefault("health", {"llm": 0, "src": {}, "alerted": []})
    s.setdefault("week", {})        # key -> {t,s,m,ts} 주간 브랜드 순위용 화제글 기록(8일 보관)
    s.setdefault("last_weekly", "")
    s.setdefault("watch_seen", {})  # 관심 종목 매칭으로 이미 처리한 글 key -> ts
    s.setdefault("watch_kw", [])    # 한 번이라도 검색한 키워드(첫 검색 결과는 기준선으로만 저장)
    s.setdefault("watch_pos", 0)    # 검색 순환 위치
    return s


def save_state(s):
    now = time.time()
    s["cands"] = {k: v for k, v in s["cands"].items() if now - v["last"] < 36 * 3600}
    for v in s["cands"].values():
        v["hist"] = v["hist"][-10:]
    s["sent"] = {k: v for k, v in s["sent"].items() if now - v < 14 * 86400}
    s["week"] = {k: v for k, v in s["week"].items() if now - v["ts"] < 8 * 86400}
    s["watch_seen"] = {k: v for k, v in s["watch_seen"].items() if now - v < 14 * 86400}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=0)


# ───────────────────────── 공통 ─────────────────────────
def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=20, **kw)
    r.raise_for_status()
    if r.encoding is None or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding
    return r


def text_of(fragment):
    s = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.I)
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"[ \t　]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def meta(h, prop):
    m = (re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % prop, h)
         or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % prop, h))
    return html.unescape(m.group(1)).strip() if m else ""


def clip(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


# ───────────────────────── 목록 수집 ─────────────────────────
def list_hatena():
    f = feedparser.parse(get("https://b.hatena.ne.jp/hotentry/all.rss").content)
    out = []
    for e in f.entries:
        try:
            n = int(e.get("hatena_bookmarkcount", 0))
        except ValueError:
            n = 0
        out.append({"key": "hb:" + e.link, "src": "hb", "title": html.unescape(e.title), "url": e.link, "metric": n,
                    "extra": {"comments_url": e.get("hatena_bookmarkcommentlistpageurl", "")}})
    return out


def list_girls():
    h = get("https://girlschannel.net/").text
    a = h.find('<ul class="topic-list">')
    seg = h[a:h.find("</ul>", a)] if a >= 0 else ""
    out = []
    for tid, n, title in re.findall(r'href="/topics/(\d+)/".*?(\d+)コメント.*?<p class="title">(.*?)</p>', seg, re.S):
        title = text_of(title)
        if GC_SKIP.search(title):
            continue
        out.append({"key": "gc:" + tid, "src": "gc", "title": title, "url": f"https://girlschannel.net/topics/{tid}/",
                    "metric": int(n), "extra": {}})
    return out


def list_togetter():
    h = get("https://togetter.com/hot").text
    out = []
    for url, title, pv in re.findall(
            r'<a href="(https://togetter\.com/li/\d+)" title="([^"]*)">.*?<span>(\d+)</span>pv', h, re.S):
        out.append({"key": "tg:" + url.rsplit("/", 1)[-1], "src": "tg", "title": html.unescape(title), "url": url,
                    "metric": int(pv), "extra": {}})
    return out


LISTERS = {"hb": list_hatena, "gc": list_girls, "tg": list_togetter}


def collect(state):
    now = time.time()
    hs = state["health"]["src"]
    for src, fn in LISTERS.items():
        try:
            rows = fn()
            if not rows:
                raise RuntimeError("목록 0건(페이지 구조 변경 가능성)")
            hs[src] = 0
        except Exception as ex:
            hs[src] = hs.get(src, 0) + 1
            print(f"[warn] {SRC[src]['name']} 목록 실패({hs[src]}회 연속): {ex}", file=sys.stderr)
            continue
        for r in rows:
            c = state["cands"].get(r["key"])
            if not c:
                c = state["cands"][r["key"]] = {"src": src, "title": r["title"], "url": r["url"], "extra": r["extra"],
                                                "hist": [], "first": now, "score": None, "ko": ""}
            c["title"], c["last"] = r["title"], now
            c["hist"].append([now, r["metric"]])
            if r["metric"] >= SRC[src]["min"] // 2:  # 주간 순위용 기록(최대 반응 수 유지)
                w = state["week"].setdefault(r["key"], {"t": r["title"], "s": src, "m": 0, "ts": now})
                w["m"] = max(w["m"], r["metric"])
        time.sleep(1)


def metric(c):
    return c["hist"][-1][1] if c["hist"] else 0


def heat(c, now):
    """최근 WINDOW_H 동안의 증가량. 구간 안에 처음 등장했으면 현재 값 전체."""
    if now - c["first"] < WINDOW_H * 3600:
        return metric(c)
    base = [m for t, m in c["hist"] if now - t >= WINDOW_H * 3600]
    return metric(c) - (base[-1] if base else c["hist"][0][1])


def eligible(state, now):
    return [(k, c) for k, c in state["cands"].items()
            if k not in state["sent"] and now - c["last"] < 2 * 3600 and metric(c) >= SRC[c["src"]]["min"]]


# ───────────────────────── 본문·댓글 수집 ─────────────────────────
def detail_hatena(c):
    body = ""
    try:
        h = get(c["url"]).text
        paras = [text_of(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", h, re.S)]
        paras = [p for p in paras if len(p) >= 30]
        body = (meta(h, "og:description") + "\n" + "\n".join(paras))[:1800]
    except Exception as ex:
        print(f"[warn] 하테나 원문 실패 {c['url']}: {ex}", file=sys.stderr)
    comments, seen = [], set()
    q = urllib.parse.quote(c["url"], safe="")
    try:  # 인기 코멘트(별 많은 순)
        d = get(f"https://b.hatena.ne.jp/api/ipad.entry_reactions?url={q}").json()
        for b in d.get("scored_bookmarks", [])[:15]:
            stars = sum(x.get("count", 0) for x in b.get("star_count", []) if isinstance(x, dict))
            if b.get("comment"):
                comments.append(f"[인기·별{stars}] {clip(b['comment'], 150)}")
                seen.add(b["comment"])
    except Exception as ex:
        print(f"[warn] 하테나 인기 코멘트 실패: {ex}", file=sys.stderr)
    try:  # 일반 코멘트(최신순)
        d = get(f"https://b.hatena.ne.jp/entry/jsonlite/?url={q}").json() or {}
        for b in (d.get("bookmarks") or []):
            if b.get("comment") and b["comment"] not in seen:
                comments.append(clip(b["comment"], 150))
            if len(comments) >= 45:
                break
    except Exception as ex:
        print(f"[warn] 하테나 코멘트 실패: {ex}", file=sys.stderr)
    return body, comments


def detail_girls(c):
    h = get(c["url"]).text
    items = re.findall(r'<li class="comment-item" id="comment(\d+)">(.*?)</li>\s*(?=<li class="comment-item"|</ul>)',
                       h, re.S)
    body, rows = "", []
    for num, block in items:
        m = re.search(r'<div class="body[^"]*">(.*?)</div>', block, re.S)
        txt = text_of(m.group(1)) if m else ""
        if num == "1":
            body = txt[:1800]
            continue
        plus = re.search(r'icon-rate-wrap-plus.*?<p>\+?(\d+)</p>', block, re.S)
        minus = re.search(r'icon-rate-wrap-minus.*?<p>-?(\d+)</p>', block, re.S)
        p, n = int(plus.group(1)) if plus else 0, int(minus.group(1)) if minus else 0
        if len(txt) >= 4:
            rows.append((p, n, txt))
    top = sorted(rows, key=lambda r: -r[0])[:25]
    contested = sorted([r for r in rows if r not in top], key=lambda r: -r[1])[:5]
    comments = [f"[+{p} / -{n}] {clip(t, 150)}" for p, n, t in top + contested]
    return body, comments


def detail_togetter(c):
    h = get(c["url"]).text
    tweets = [text_of(t) for t in re.findall(r"<p class='tweet[^']*'>(.*?)</p>", h, re.S)]
    heads = [text_of(x) for x in re.findall(r'<h2 class="md-h-1"[^>]*>(.*?)<button', h, re.S)]
    first = tweets[0] if tweets else ""
    body = (first + ("\n[정리 소제목] " + " / ".join(heads) if heads else "")
            + "\n[설명] " + meta(h, "og:description"))[:1800]
    comments = [clip(t, 150) for t in tweets[1:41] if len(t) >= 4]
    return body, comments


DETAIL = {"hb": detail_hatena, "gc": detail_girls, "tg": detail_togetter}


def fetch_detail(c):
    try:
        b, cm = DETAIL[c["src"]](c)
    except Exception as ex:
        print(f"[warn] 상세 수집 실패 {c['url']}: {ex}", file=sys.stderr)
        b, cm = "", []
    time.sleep(1)
    return b, cm


# ───────────────────────── LLM ─────────────────────────
TRIAGE_PROMPT = """너는 한국 자산운용사의 일본 주식 담당 애널리스트를 돕는 데스크다.
일본 커뮤니티(하테나 북마크·걸즈채널·토게터)에서 화제인 글 제목들이다. 각 글의 '주식 투자 관련성'을 매겨 JSON 배열만 출력하라.

score 기준:
5 = 상장사 주가를 직접 움직일 만한 사안이 커뮤니티에서 확산: 대형 불매운동, 대규모 리콜·식품 안전 사고,
    개인정보 대량 유출, 기업 경영진 스캔들, 대형 서비스 장애.
4 = 특정 상장사·업종에 의미 있는 여론: 신제품·가격 인상에 대한 강한 반발이나 열광, 브랜드 평판 훼손 논란,
    소비 행태 변화의 뚜렷한 신호, 정책(세제·연금·보조금)에 대한 대중 반발.
3 = 기업·브랜드가 언급되는 일반 화제, 소비 트렌드 잡담.
2 = 사회·생활 화제.  1 = 연예·취미·잡담.
4 이상은 드물다(전체의 5% 안팎). 확실하지 않으면 3 이하로 매겨라.

출력 필드: id, score(1~5 정수), ko_title(정확한 한국어 제목 한 줄). 고유명사는 정확히 옮겨라. JSON 외 텍스트 금지.

글:
{items}
"""

WRITE_PROMPT = """너는 한국 자산운용사의 일본 주식 담당 애널리스트를 돕는 데스크다.
일본 커뮤니티 화제글의 본문과 댓글(일본어)을 한국어로 정리해 JSON 배열만 출력하라. 독자는 일본어를 못 읽는다.
일본어를 그대로 쓰지 말고 모두 한국어로 옮겨라(고유명사도 한글로, 예: 千葉西総合病院=지바니시종합병원).

글마다 출력:
 id
 ko_title: 정확한 한국어 제목 한 줄.
 body_points: 글 내용 요약 최대 2개. 각 40자 이내의 짧은 1문장, '~함/~했음' 보고서체. 핵심만 쓰고 배경 설명은 빼라.
              본문에 있는 사실만, 없는 숫자를 만들지 마라.
 comment_points: 댓글 반응 요약 최대 2개. 각 40자 이내의 짧은 1문장. 첫째는 다수·공감(+·별) 많은 의견,
                 둘째는 반대·소수 의견(없으면 생략). 인용은 하지 마라. 댓글이 없으면 빈 배열.
 note: 상장사·업종·소비 트렌드와 연결되는 투자 시사점이 있을 때만 '(추정)'으로 시작하는 50자 이내 1문장. 없으면 빈 문자열.
 companies: 글에 직접 등장하는 일본·한국 상장 기업(최대 4개). 각 항목 {{"ko": 한국어 기업명,
            "official": 상장 정식 사명(일본 기업은 일본어 정식 사명), "market": "JP" 또는 "KR"}}. 확실하지 않으면 넣지 마라.
 related_news: 아래 NEWS(최근 뉴스봇이 보낸 기사) 중 이 글과 '같은 사건'을 다룬 기사의 id. 없거나 애매하면 빈 문자열.
JSON 외 텍스트 금지.

NEWS:
{news}

글:
{items}
"""

OVERVIEW_PROMPT = """너는 한국 자산운용사의 일본 주식 담당 애널리스트를 돕는 데스크다.
아래는 최근 6시간 동안 일본 커뮤니티(하테나 북마크·걸즈채널·토게터)에서 반응이 늘어난 글 제목과 반응 수다.
(따로 상세 요약하는 상위 글은 제외했다.) 시황 정리처럼 '이번 6시간 커뮤니티 동향'을 정확히 10줄로 정리해 JSON 문자열 배열만 출력하라.

규칙:
- 각 줄은 '[주제] 내용' 형식, 50자 이내 1문장, '~함/~이어짐' 보고서체. 주제 예: 사회, 정치·정책, 기업·소비, IT, 연예, 생활.
- 개별 글 나열이 아니라 비슷한 글을 묶어 흐름을 써라. 반응 수가 많은 흐름부터 쓴다.
- 여러 사이트에서 함께 화제인 주제는 그렇게 밝혀라.
- 제목에 있는 사실만 쓰고 추측하지 마라. 일본어를 쓰지 말고 고유명사도 한글로 옮겨라.
JSON 외 텍스트 금지.

글 목록:
{items}
"""
OVERVIEW_SCHEMA = {"type": "ARRAY", "items": {"type": "STRING"}}

TRIAGE_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "STRING"}, "score": {"type": "INTEGER"}, "ko_title": {"type": "STRING"}},
    "required": ["id", "score", "ko_title"]}}
WRITE_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "STRING"}, "ko_title": {"type": "STRING"},
    "body_points": {"type": "ARRAY", "items": {"type": "STRING"}},
    "comment_points": {"type": "ARRAY", "items": {"type": "STRING"}},
    "note": {"type": "STRING"}, "related_news": {"type": "STRING"},
    "companies": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "ko": {"type": "STRING"}, "official": {"type": "STRING"}, "market": {"type": "STRING"}}}}},
    "required": ["id", "ko_title", "body_points", "comment_points"]}}


def triage(state, pairs):
    lines = "\n".join(json.dumps({"id": f"c{i}", "src": SRC[c["src"]]["name"], "title": c["title"]},
                                 ensure_ascii=False) for i, (_, c) in enumerate(pairs))
    res = bot.gemini(state, TRIAGE_PROMPT.format(items=lines), TRIAGE_SCHEMA, bot.TRIAGE_MODELS, "커뮤니티 선별")
    by = {r["id"]: r for r in res if isinstance(r, dict) and "id" in r}
    for i, (_, c) in enumerate(pairs):
        r = by.get(f"c{i}")
        c["score"] = int(r.get("score") or 1) if r else 1
        c["ko"] = (r or {}).get("ko_title", "")


def write_up(state, pairs):
    items = []
    for i, (_, c) in enumerate(pairs):
        body, comments = fetch_detail(c)
        items.append(json.dumps({"id": f"w{i}", "src": SRC[c["src"]]["name"], "title": c["title"],
                                 "body": body or "(본문 수집 실패: 제목만으로 요약)", "comments": comments},
                                ensure_ascii=False))
    news = recent_news()
    res = bot.gemini(state, WRITE_PROMPT.format(
        items="\n".join(items), news="\n".join(f"{k}: {v}" for k, v in news.items()) or "(없음)"),
        WRITE_SCHEMA, bot.WRITE_MODELS + bot.TRIAGE_MODELS, "커뮤니티 요약")
    by = {r["id"]: r for r in res if isinstance(r, dict) and "id" in r}
    out = [by.get(f"w{i}") or {} for i in range(len(pairs))]
    for w in out:  # 연계 뉴스 id → 뉴스 제목
        w["news_title"] = news.get((w.get("related_news") or "").strip(), "")
    return out


def recent_news():
    """뉴스봇(state.json)이 최근 24시간 안에 채널로 보낸 기사 {id: 한국어 제목}."""
    try:
        with open(bot.STATE_PATH, encoding="utf-8") as f:
            rec = json.load(f).get("recent", [])
    except Exception:
        return {}
    now = time.time()
    return {r["id"]: r.get("ko", "") for r in rec[-300:] if r.get("sent") and now - r.get("ts", 0) < 86400}


def overview(state, picks, now):
    """상위 글 외에 지난 6시간 동안 반응이 늘어난 글들을 10줄 동향으로 정리. 실패하면 빈 목록."""
    picked = {k for k, _ in picks}
    rows = []
    for src in SRC:
        pool = [c for k, c in state["cands"].items()
                if c["src"] == src and k not in picked and now - c["last"] < 2 * 3600 and heat(c, now) > 0]
        pool.sort(key=lambda c: -heat(c, now))
        rows += [json.dumps({"src": SRC[src]["name"], "title": c["title"], SRC[src]["unit"]: heat(c, now)},
                            ensure_ascii=False) for c in pool[:40]]
    if len(rows) < 10:
        return []
    try:
        res = bot.gemini(state, OVERVIEW_PROMPT.format(items="\n".join(rows)), OVERVIEW_SCHEMA,
                         bot.WRITE_MODELS + bot.TRIAGE_MODELS, "커뮤니티 동향")
        return [str(x).strip() for x in res if str(x).strip()][:10]
    except Exception as ex:
        print(f"[warn] 동향 정리 실패(생략하고 발송): {ex}", file=sys.stderr)
        return []


# ───────────────────────── 텔레그램 ─────────────────────────
def tg_send(text, silent=False, chat=None):
    if DRY_RUN:
        print(f"──── SEND{' (무음)' if silent else ''} ────\n{text}")
        return True
    tok = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = chat or os.environ["TELEGRAM_CHAT_ID"]
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True, "disable_notification": silent}, timeout=20)
        if r.status_code == 200:
            time.sleep(1.1)
            return True
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        print(f"[warn] telegram {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    return False


def fmt_item(c, w, idx=None):
    s = SRC[c["src"]]
    title = w.get("ko_title") or c.get("ko") or c["title"]
    num = f"{idx}. " if idx else ""
    lines = [f"<b>{num}{esc(title)}</b> ({s['unit']} {metric(c):,})"]
    if w.get("body_points"):
        lines.append("\n<b>[내용]</b>")
        lines += [f"• {esc(b.lstrip('•· ').strip())}" for b in w["body_points"][:2]]
    if w.get("comment_points"):
        lines.append("\n<b>[댓글]</b>")
        lines += [f"• {esc(b.lstrip('•· ').strip())}" for b in w["comment_points"][:2]]
    if (w.get("note") or "").strip():
        lines.append(f"\n{esc(w['note'].strip())}")
    if w.get("news_title"):
        lines.append(f"\n<b>[뉴스 연계]</b> {esc(w['news_title'])}")
    cos = bot.company_line(w.get("companies"))
    if cos:
        lines.append(f"언급 기업: {esc(cos)}")
    links = f"<a href=\"{esc(c['url'])}\">원문</a>"
    if c["extra"].get("comments_url"):
        links += f" · <a href=\"{esc(c['extra']['comments_url'])}\">댓글</a>"
    lines.append(f"\n&gt;{links}")
    return "\n".join(lines)


def send_chunks(header, blocks, silent, sep="\n\n━━━━━━━━━━\n"):
    chunks, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > 3800:
            chunks.append(cur)
            cur = ""
        cur += (sep if cur else "") + b
    chunks.append(cur)
    return all(tg_send(x, silent) for x in chunks)


# ───────────────────────── 즉시 알림(★4 이상) ─────────────────────────
def urgent_pass(state, now, first_run):
    todo = [(k, c) for k, c in eligible(state, now) if c.get("score") is None]
    if not todo:
        return None
    if first_run:  # 첫 실행: 지금 떠 있는 글은 즉시 알림 대상에서 제외(기준선)
        for _, c in todo:
            c["score"] = 0
        return None
    try:
        triage(state, todo[:60])
    except Exception as ex:
        print(f"[warn] 커뮤니티 선별 실패, 다음 실행에 재시도: {ex}", file=sys.stderr)
        return False
    hot = [(k, c) for k, c in todo if (c.get("score") or 0) >= THRESHOLD]
    if hot:
        try:
            ws = write_up(state, hot)
        except Exception as ex:
            print(f"[warn] 즉시 알림 요약 실패: {ex}", file=sys.stderr)
            ws = [{} for _ in hot]
        for (k, c), w in zip(hot, ws):
            head = f"<b>&gt;&gt;[커뮤니티 ★{c['score']}]</b> {SRC[c['src']]['name']}\n"
            if tg_send(head + fmt_item(c, w)):
                state["sent"][k] = time.time()
                save_state(state)
    return True


# ───────────────────────── 다이제스트 ─────────────────────────
def due_slot(now_dt):
    t = now_dt + timedelta(minutes=LEAD_MIN)
    h = max(s for s in SLOTS if s <= t.hour)
    slot_dt = t.replace(hour=h, minute=0, second=0, microsecond=0)
    return slot_dt.strftime("%Y-%m-%d %H:00"), slot_dt


def pick(state, now):
    out = []
    for src in SRC:
        pool = [(k, c) for k, c in eligible(state, now) if c["src"] == src]
        pool.sort(key=lambda kc: -heat(kc[1], now))
        out += [kc for kc in pool if heat(kc[1], now) > 0][:PER_SOURCE]
    return out


def digest_pass(state, now_dt, force):
    key, slot_dt = due_slot(now_dt)
    if state["last_slot"] == key and not force:
        return None
    now = time.time()
    picks = pick(state, now)
    if not picks:
        print(f"[info] {key} 다이제스트: 기준을 넘는 새 글 없음 → 건너뜀")
        state["last_slot"], state["slot_fail_since"] = key, 0
        return None
    try:
        ws = write_up(state, picks)
        ok_llm = True
    except Exception as ex:
        print(f"[warn] 다이제스트 요약 실패: {ex}", file=sys.stderr)
        state["slot_fail_since"] = state["slot_fail_since"] or now
        if now - state["slot_fail_since"] < FALLBACK_AFTER_H * 3600 and not force:
            return False  # 다음 실행에 재시도
        ws, ok_llm = [{} for _ in picks], False
    silent = slot_dt.hour in SILENT_SLOTS
    header = (f"<b>&gt;&gt;[커뮤니티] 일본 커뮤니티 화제글</b> {slot_dt.strftime('%m-%d %H:%M')} ({len(picks)}건)"
              + ("" if ok_llm else "\n(요약 생성 실패: 원제와 링크만 보냅니다)"))
    blocks, cur_src, n = [], None, 0
    trend = overview(state, picks, now) if ok_llm else []
    if trend:
        blocks.append("<b>[6시간 동향]</b>\n" + "\n".join(f"• {esc(t.lstrip('•· '))}" for t in trend))
    for (k, c), w in zip(picks, ws):
        if c["src"] != cur_src:
            cur_src, n = c["src"], 0
        n += 1
        b = fmt_item(c, w, n)
        blocks.append(f"<b>[{SRC[cur_src]['name']}]</b>\n{b}" if n == 1 else b)
    if send_chunks(header, blocks, silent):
        for k, _ in picks:
            state["sent"][k] = now
        state["last_slot"], state["slot_fail_since"] = key, 0
    return ok_llm


# ───────────────────────── 관심 종목 키워드 알림 ─────────────────────────
WATCH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.csv")
WATCH_SEARCH_N = int(os.getenv("WATCH_SEARCH_N", "6"))   # 실행 1회당 검색할 키워드 수(순환)
WATCH_MIN = {"hb": int(os.getenv("WATCH_HATENA_MIN", "10")), "gc": int(os.getenv("WATCH_GIRLS_MIN", "30")), "tg": 0}
WATCH_MAX_AGE_H = 48
QUIET_HOURS = range(0, 7)        # 이 시간대(KST) 관심 종목 알림은 무음
_NAME_SUFFIX = re.compile(r"(ホールディングス|ホールディング|ＨＤ|HD|holdings|holding|グループ|group|株式会社|\(株\)|（株）|"
                          r"コーポレーション|corporation|inc\.?|co\.,?ltd\.?)", re.I)


def wnorm(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).lower()


def load_watch():
    """watchlist.csv → [(티커, 정식 사명, [정규화 키워드])]. 키워드 칸이 비면 상장 사명에서 자동 생성."""
    names = {}
    try:
        with open(os.path.join(bot.DATA_DIR, "jpx_list.csv"), encoding="utf-8") as f:
            names = {r["code"]: r["name"] for r in csv.DictReader(f)}
    except FileNotFoundError:
        pass
    out = []
    try:
        with open(WATCH_PATH, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    except FileNotFoundError:
        return out
    for row in csv.DictReader(lines):
        code = (row.get("ticker") or "").strip().upper()
        if not code:
            continue
        official = names.get(code, "")
        kws = [k.strip() for k in (row.get("keywords") or "").split("|") if k.strip()]
        auto = wnorm(_NAME_SUFFIX.sub("", unicodedata.normalize("NFKC", official)))
        if auto and len(auto) >= 2:
            kws.insert(0, auto)
        kws = list(dict.fromkeys(wnorm(k) for k in kws if len(wnorm(k)) >= 2))
        if kws:
            out.append((code, official, kws))
        else:
            print(f"[warn] 관심 종목 {code}: 상장 목록에 없고 키워드도 없어 건너뜀", file=sys.stderr)
    return out


def search_hatena(kw):
    q = urllib.parse.quote(kw)
    f = feedparser.parse(get(f"https://b.hatena.ne.jp/q/{q}?target=entry&sort=recent&users=3&mode=rss").content)
    out = []
    for e in f.entries:
        t = e.get("updated_parsed") or e.get("published_parsed")
        out.append({"key": "hb:" + e.link, "src": "hb", "title": html.unescape(e.title), "url": e.link,
                    "metric": int(e.get("hatena_bookmarkcount", 0) or 0),
                    "ts": calendar.timegm(t) if t else time.time()})
    return out


def search_girls(kw):
    h = get(f"https://girlschannel.net/topics/search/?q={urllib.parse.quote(kw)}").text
    a = h.find('<ul class="topic-list">')
    seg = h[a:h.find("</ul>", a)] if a >= 0 else ""
    out = []
    for tid, n, dt, title in re.findall(
            r'href="/topics/(\d+)/".*?(\d+)コメント.*?<span class="datetime">([^<]*)</span>.*?<p class="title">(.*?)</p>',
            seg, re.S):
        # 검색 목록의 시각은 마지막 댓글 시각이라 작성 시각은 매칭된 글만 따로 확인(girls_created)
        out.append({"key": "gc:" + tid, "src": "gc", "title": text_of(title),
                    "url": f"https://girlschannel.net/topics/{tid}/", "metric": int(n), "ts": None})
    return out


def girls_created(url):
    """걸즈채널 토픽 작성 시각(1번 글 시각). 실패하면 None."""
    try:
        h = get(url).text
        i = h.find('id="comment1"')
        m = re.search(r"(\d{4})/(\d{2})/(\d{2})\([^)]*\)\s*(\d{2}):(\d{2})", h[i:i + 600])
        return datetime(*map(int, m.groups()), tzinfo=JST).timestamp() if m else None
    except Exception:
        return None


WATCH_PROMPT = """일본 커뮤니티 글 제목을 한국어로 옮겨 JSON 배열만 출력하라. 독자는 일본어를 못 읽는다.
글마다: id, ko_title(정확한 한국어 제목 한 줄, 고유명사도 한글로), company_ko(해당 기업의 한국어 통용명).
JSON 외 텍스트 금지.

{items}
"""
WATCH_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "STRING"}, "ko_title": {"type": "STRING"}, "company_ko": {"type": "STRING"}},
    "required": ["id", "ko_title"]}}


def watch_pass(state, now_dt, first_run):
    watch = load_watch()
    if not watch:
        return None
    now = time.time()
    hits = {}  # key -> (글, 티커, 사명)

    def check(row):
        if row["key"] in state["watch_seen"] or row["key"] in hits:
            return
        if row["metric"] < WATCH_MIN[row["src"]]:
            return
        if row.get("ts") is not None and now - row["ts"] > WATCH_MAX_AGE_H * 3600:
            return
        t = wnorm(row["title"])
        for code, official, kws in watch:
            if any(k in t for k in kws):
                if row.get("ts") is None:  # 작성 시각 확인이 필요한 글(걸즈채널 검색 결과)
                    row["ts"] = girls_created(row["url"])
                    if row["ts"] is None or now - row["ts"] > WATCH_MAX_AGE_H * 3600:
                        state["watch_seen"][row["key"]] = now  # 오래된 글: 다시 확인하지 않음
                        return
                hits[row["key"]] = (row, code, official)
                return

    # 1) 이번에 수집한 인기 목록 전체(기준 미달 글 포함)
    for k, c in state["cands"].items():
        if now - c["last"] < 600:
            check({"key": k, "src": c["src"], "title": c["title"], "url": c["url"], "metric": metric(c),
                   "ts": c["first"]})
    if first_run:  # 첫 실행: 지금 떠 있는 글은 기준선
        for k in hits:
            state["watch_seen"][k] = now
        hits.clear()
    # 2) 키워드 검색(하테나·걸즈채널), 실행마다 몇 개씩 순환
    allkw = [(code, k) for code, _, kws in watch for k in kws]
    pos = state["watch_pos"] % len(allkw)
    batch = (allkw[pos:] + allkw[:pos])[:WATCH_SEARCH_N]
    state["watch_pos"] = pos + len(batch)
    for code, kw in batch:
        baseline = kw not in state["watch_kw"]
        rows = []
        for fn in (search_hatena, search_girls):
            try:
                rows += fn(kw)
            except Exception as ex:
                print(f"[warn] 관심 종목 검색 실패({fn.__name__}, {kw}): {ex}", file=sys.stderr)
            time.sleep(1)
        before = set(hits)
        for r in rows:
            check(r)
        if baseline:  # 처음 검색한 키워드는 기존 글을 기준선으로만 저장
            for k in set(hits) - before:
                state["watch_seen"][k] = now
                del hits[k]
            state["watch_kw"].append(kw)
    if not hits:
        return None
    items = list(hits.values())
    try:
        res = bot.gemini(state, WATCH_PROMPT.format(items="\n".join(
            json.dumps({"id": f"k{i}", "title": r["title"], "company": off or code}, ensure_ascii=False)
            for i, (r, code, off) in enumerate(items))), WATCH_SCHEMA, bot.TRIAGE_MODELS, "관심 종목")
        by = {x["id"]: x for x in res if isinstance(x, dict) and "id" in x}
        ok = True
    except Exception as ex:
        print(f"[warn] 관심 종목 번역 실패(원제로 발송): {ex}", file=sys.stderr)
        by, ok = {}, False
    groups = {}
    for i, (r, code, off) in enumerate(items):
        x = by.get(f"k{i}", {})
        groups.setdefault(code, {"name": x.get("company_ko") or off or code, "rows": []})
        groups[code]["rows"].append((r, x.get("ko_title") or r["title"]))
    lines = [f"<b>&gt;&gt;[관심 종목] 커뮤니티 언급 {len(items)}건</b>"]
    for code, g in groups.items():
        lines.append(f"\n<b>{esc(g['name'])}({esc(code)} JP)</b>")
        for r, ko in sorted(g["rows"], key=lambda z: -z[0]["metric"]):
            s = SRC[r["src"]]
            lines.append(f"• <a href=\"{esc(r['url'])}\">{esc(ko)}</a> ({s['name']} · {s['unit']} {r['metric']:,})")
    blocks = ["\n".join(lines[i:i + 1]) for i in range(1, len(lines))]
    if send_chunks(lines[0], blocks, now_dt.hour in QUIET_HOURS, sep="\n"):
        for k in hits:
            state["watch_seen"][k] = now
    return ok


# ───────────────────────── 주간 브랜드·기업 언급 순위 ─────────────────────────
WEEKLY_PROMPT = """너는 한국 자산운용사의 일본 소비재·산업재 담당 애널리스트를 돕는 데스크다.
아래는 지난 7일 동안 일본 커뮤니티(하테나 북마크·걸즈채널·토게터)에서 화제가 된 글 제목과 반응 수다.
제목에 등장하는 기업·브랜드를 뽑아 JSON 배열만 출력하라(최대 15개).

항목마다:
 name_ko: 한국어 이름. 브랜드면 '브랜드(모회사)' 형식(예: 유니클로(패스트리테일링)).
 official: 상장 모회사의 일본어 정식 사명(비상장이거나 모르면 빈 문자열). market: "JP" 또는 "KR".
 ids: 그 기업·브랜드가 제목에 실제로 등장하는 글 id 목록. 제목에 없는 글은 넣지 마라.
 tone: 여론 분위기. '긍정', '부정', '혼재' 중 하나.
 gist: 무엇이 화제였는지 40자 이내 1문장, '~함' 보고서체. 일본어 금지.
방송국·정당·관공서·스포츠 구단은 제외하고 기업·브랜드만 뽑아라. JSON 외 텍스트 금지.

글:
{items}
"""
WEEKLY_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "name_ko": {"type": "STRING"}, "official": {"type": "STRING"}, "market": {"type": "STRING"},
    "ids": {"type": "ARRAY", "items": {"type": "STRING"}}, "tone": {"type": "STRING"}, "gist": {"type": "STRING"}},
    "required": ["name_ko", "ids", "tone", "gist"]}}


def weekly_pass(state, slot_dt, force=False):
    """일요일 18시 회차에 지난 7일 브랜드·기업 언급 순위를 보낸다."""
    key = slot_dt.strftime("%Y-%m-%d")
    if not force and (slot_dt.weekday() != 6 or slot_dt.hour != 18 or state["last_weekly"] == key):
        return None
    now = time.time()
    rows = sorted([v for v in state["week"].values() if now - v["ts"] < 7 * 86400], key=lambda v: -v["m"])[:400]
    if len(rows) < 20:
        state["last_weekly"] = key
        return None
    ids = {f"t{i}": r for i, r in enumerate(rows)}
    try:
        res = bot.gemini(state, WEEKLY_PROMPT.format(items="\n".join(
            json.dumps({"id": i, "src": SRC[r["s"]]["name"], "title": r["t"], SRC[r["s"]]["unit"]: r["m"]},
                       ensure_ascii=False) for i, r in ids.items())), WEEKLY_SCHEMA,
            bot.WRITE_MODELS + bot.TRIAGE_MODELS, "주간 순위")
    except Exception as ex:
        print(f"[warn] 주간 순위 실패, 다음 실행에 재시도: {ex}", file=sys.stderr)
        return False
    ranked = []
    for b in res:
        if not isinstance(b, dict):
            continue
        valid = [i for i in dict.fromkeys(b.get("ids") or []) if i in ids]
        if valid:
            ranked.append((len(valid), len({ids[i]["s"] for i in valid}), b))
    ranked.sort(key=lambda z: (-z[0], -z[1]))
    start = datetime.fromtimestamp(now - 7 * 86400, JST).strftime("%m/%d")
    lines = [f"<b>&gt;&gt;[커뮤니티 주간] 기업·브랜드 언급 순위</b> {start}~{slot_dt.strftime('%m/%d')}",
             f"(화제글 {len(rows)}건 기준)"]
    for n, (cnt, nsrc, b) in enumerate(ranked[:10], 1):
        tick = bot.company_line([{"ko": b["name_ko"], "official": b.get("official", ""), "market": b.get("market", "JP")}])
        name = tick or b["name_ko"]
        lines.append(f"\n<b>{n}. {esc(name)}</b> — {cnt}건 · {esc(b.get('tone', ''))}")
        lines.append(f"• {esc(b.get('gist', ''))}")
    if not ranked:
        lines.append("\n이번 주에는 기업·브랜드 화제가 뚜렷하지 않았습니다.")
    if send_chunks("\n".join(lines[:2]), lines[2:], False, sep="\n"):
        state["last_weekly"] = key
    return True


# ───────────────────────── 장애 알림 ─────────────────────────
def check_health(state, llm_ok):
    h = state["health"]
    if llm_ok is not None:
        h["llm"] = 0 if llm_ok else h.get("llm", 0) + 1
    problems = {}
    if h.get("llm", 0) >= FAIL_ALERT_N:
        problems["llm"] = f"Gemini 처리 {h['llm']}회 연속 실패"
    for src, n in h["src"].items():
        if n >= FAIL_ALERT_N:
            problems[src] = f"{SRC[src]['name']} 목록 수집 {n}회 연속 실패(사이트 구조 변경 가능성)"
    alerted = set(h.get("alerted", []))
    new = [k for k in problems if k not in alerted]
    fixed = [k for k in alerted if k not in problems]
    admin = os.getenv("TELEGRAM_ADMIN_CHAT_ID") or None
    if new:
        tg_send("<b>[봇 경고]</b> 일본 커뮤니티봇\n" + "\n".join(f"• {esc(problems[k])}" for k in new)
                + "\n로그: GitHub Actions → jp-community-bot", chat=admin)
    if fixed:
        tg_send("<b>[복구]</b> 일본 커뮤니티봇\n" + "\n".join(
            f"• {'Gemini' if k == 'llm' else SRC.get(k, {}).get('name', k)} 정상화" for k in fixed), chat=admin)
    h["alerted"] = list(problems)


# ───────────────────────── 메인 ─────────────────────────
def main():
    state = load_state()
    first_run = not state["cands"]
    now_dt = datetime.now(JST)
    collect(state)
    save_state(state)
    now = time.time()
    if first_run:
        state["last_slot"] = due_slot(now_dt)[0]  # 첫 실행 직후 바로 다이제스트를 보내지 않음
    r1 = urgent_pass(state, now, first_run)
    save_state(state)
    r3 = watch_pass(state, now_dt, first_run)
    save_state(state)
    r2 = digest_pass(state, now_dt, os.getenv("DIGEST_NOW") == "1")
    save_state(state)
    r4 = weekly_pass(state, due_slot(now_dt)[1], os.getenv("WEEKLY_NOW") == "1")
    results = [r for r in (r1, r2, r3, r4) if r is not None]
    check_health(state, all(results) if results else None)
    save_state(state)


if __name__ == "__main__":
    main()
