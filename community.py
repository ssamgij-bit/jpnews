"""일본 커뮤니티 화제글 → 한국어 요약(본문·댓글) → 텔레그램 채널 다이제스트.

소스: 하테나 북마크 인기 엔트리(종합), 걸즈채널 오늘의 인기 토픽, 토게터 주목 정리.
매시 실행(GitHub Actions)으로 화제도 스냅샷을 쌓고, 00·06·12·18시(JST=KST)에 다이제스트를 보낸다.
00·06시는 무음 발송. 상장사·증시 관련성 ★4 이상은 다이제스트를 기다리지 않고 즉시 보낸다.
상태는 community_state.json(워크플로우가 커밋). 뉴스봇(bot.py)의 Gemini·티커 대조 함수를 재사용한다.
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY
선택: TELEGRAM_ADMIN_CHAT_ID, COMMUNITY_THRESHOLD(기본 4), HATENA_MIN(100), GIRLS_MIN(300), TOGETTER_MIN(20000),
      PER_SOURCE(2),
      DIGEST_NOW=1(지금 바로 다이제스트), WEEKLY_NOW=1(지금 바로 주간 순위), DRY_RUN=1
일요일 18시 회차에 주간 기업·브랜드 언급 순위와 투자 아이디어·인사이트.
다이제스트 회차마다 X 트렌드(trends24 일본) 최근 6시간 상위 10과 화제 이유를 별도 메시지로. X_NOW=1(지금 바로)
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

import feedparser
import requests

import bot  # 같은 저장소의 뉴스봇: gemini(), company_line(), esc(), JST, UA 재사용
from bot import JST, UA, esc

# 커뮤니티봇은 다이제스트·동향·X 트렌드·주간 정리까지 한 번에 돌 수 있어 시간 예산을 넉넉히(워크플로우 제한 12분)
bot.RUN_BUDGET = int(os.getenv("RUN_BUDGET", "560"))

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
    for k in ("watch_seen", "watch_kw", "watch_pos"):  # 관심 종목 기능 폐지: 남은 기록 삭제
        s.pop(k, None)
    s.setdefault("xweek", [])       # 주간 정리용 X 트렌드 기록 [{ts, ko, cat, reason}]
    return s


def save_state(s):
    now = time.time()
    s["cands"] = {k: v for k, v in s["cands"].items() if now - v["last"] < 36 * 3600}
    for v in s["cands"].values():
        v["hist"] = v["hist"][-10:]
    s["sent"] = {k: v for k, v in s["sent"].items() if now - v < 14 * 86400}
    s["week"] = {k: v for k, v in s["week"].items() if now - v["ts"] < 8 * 86400}
    s["xweek"] = [x for x in s["xweek"] if now - x["ts"] < 8 * 86400][-400:]
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
일본 고유명사는 일본어 발음대로 한글 표기하라(예: 第一興商=다이이치코쇼, 한자의 한국식 독음 금지).

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
- 제목에 있는 사실만 쓰고 추측하지 마라. 일본어를 쓰지 말고 고유명사도 한글로 옮겨라. 일본 고유명사는 일본어 발음대로 한글 표기하라(예: 第一興商=다이이치코쇼, 한자의 한국식 독음 금지).
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


# ───────────────────────── 주간 브랜드·기업 언급 순위 ─────────────────────────
WEEKLY_PROMPT = """너는 한국 자산운용사의 일본 소비재·산업재 담당 애널리스트를 돕는 데스크다.
아래는 지난 7일 동안 일본 커뮤니티(하테나 북마크·걸즈채널·토게터)에서 화제가 된 글 제목과 반응 수다.
제목에 등장하는 기업·브랜드를 뽑아 JSON 배열만 출력하라(최대 15개).

항목마다:
 name_ko: 한국어 이름. 브랜드면 '브랜드(모회사)' 형식(예: 유니클로(패스트리테일링)).
 official: 상장 모회사의 일본어 정식 사명(비상장이거나 모르면 빈 문자열). market: "JP" 또는 "KR".
 ids: 그 기업·브랜드가 제목에 실제로 등장하는 글 id 목록. 제목에 없는 글은 넣지 마라.
 tone: 여론 분위기. '긍정', '부정', '혼재' 중 하나.
 gist: 무엇이 화제였는지 40자 이내 1문장, '~함' 보고서체. 일본어 금지. 일본 고유명사는 일본어 발음대로 한글 표기하라(예: 第一興商=다이이치코쇼, 한자의 한국식 독음 금지).
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
        weekly_insight(state, rows, ranked, ids, start, slot_dt)
    return True


INSIGHT_PROMPT = """너는 한국 자산운용사 주식운용본부의 일본 주식 담당 애널리스트를 돕는 리서치 데스크다.
아래는 지난 7일 일본 커뮤니티(하테나 북마크·걸즈채널·토게터) 화제글, 기업·브랜드 언급 순위, X(트위터) 트렌드다.
사람들의 관심 변화에서 나올 수 있는 투자 아이디어·인사이트를 3~5개 정리해 JSON 배열만 출력하라.
소비재에 한정하지 말고 산업재·IT·금융·정책·인바운드·엔터테인먼트 등 어디든 좋다.

항목마다:
 theme: 아이디어 제목(25자 이내).
 observation: 관찰된 사실 1~2문장, '~함' 보고서체. 위 데이터에 실제로 있는 화제만 근거로 쓰고, 몇 건·어느 사이트인지 밝혀라.
 implication: 투자 시사점 1~2문장, '~함' 보고서체. 반드시 '(추정)'으로 시작한다. 수혜·피해 방향과 이유를 쓴다.
 companies: 관련 일본·한국 상장 기업(최대 4개) [{{"ko": 한국어 기업명, "official": 일본어 정식 사명(한국 기업은 한국어), "market": "JP" 또는 "KR"}}]. 확실하지 않으면 빈 배열.
 sectors: 관련 업종 한국어 1~3개.
 counter: 반대 논거·리스크 1문장, '~함' 보고서체(일시적 유행, 표본 편향, 이미 주가 반영 등).
규칙: 커뮤니티·SNS 반응은 표본이 편향된 보조 지표임을 전제로 과장하지 마라. 데이터에 없는 수치를 만들지 마라.
일본어를 쓰지 말고 고유명사도 한글로 옮겨라. 일본 고유명사는 일본어 발음대로 한글 표기하라(예: 第一興商=다이이치코쇼, 한자의 한국식 독음 금지). JSON 외 텍스트 금지.

[기업·브랜드 언급 순위]
{brands}

[화제글 상위(반응 수 순)]
{posts}

[X 트렌드(주간 등장 횟수 순)]
{xtrends}
"""
INSIGHT_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "theme": {"type": "STRING"}, "observation": {"type": "STRING"}, "implication": {"type": "STRING"},
    "companies": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "ko": {"type": "STRING"}, "official": {"type": "STRING"}, "market": {"type": "STRING"}}}},
    "sectors": {"type": "ARRAY", "items": {"type": "STRING"}}, "counter": {"type": "STRING"}},
    "required": ["theme", "observation", "implication", "counter"]}}


def weekly_insight(state, rows, ranked, ids, start, slot_dt):
    """주간 순위 뒤에 '이번 주 투자 아이디어·인사이트'를 별도 메시지로. 실패하면 생략."""
    now = time.time()
    brands = "\n".join(f"- {b['name_ko']}: {cnt}건, {b.get('tone', '')}, {b.get('gist', '')}"
                       for cnt, _, b in ranked[:15]) or "(없음)"
    posts = "\n".join(f"- [{SRC[r['s']]['name']} {SRC[r['s']]['unit']} {r['m']}] {r['t']}" for r in rows[:150])
    cnt = {}
    for x in state["xweek"]:
        if now - x["ts"] < 7 * 86400:
            c = cnt.setdefault(x["ko"], {"n": 0, "cat": x.get("cat", ""), "reason": x.get("reason", "")})
            c["n"] += 1
    xtrends = "\n".join(f"- {k} ({v['n']}회, {v['cat']}): {v['reason']}"
                         for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]["n"])[:40]) or "(기록 없음)"
    try:
        res = bot.gemini(state, INSIGHT_PROMPT.format(brands=brands, posts=posts, xtrends=xtrends), INSIGHT_SCHEMA,
                         bot.WRITE_MODELS + bot.TRIAGE_MODELS, "주간 인사이트")
    except Exception as ex:
        print(f"[warn] 주간 인사이트 실패(생략): {ex}", file=sys.stderr)
        return
    ideas = [x for x in res if isinstance(x, dict) and x.get("theme")][:5]
    if not ideas:
        return
    head = f"<b>&gt;&gt;[커뮤니티 주간] 투자 아이디어·인사이트</b> {start}~{slot_dt.strftime('%m/%d')}"
    blocks = []
    for n, x in enumerate(ideas, 1):
        lines = [f"<b>&gt;{n}. {esc(x['theme'])}</b>", f"•관찰: {esc(x['observation'])}", f"•{esc(x['implication'])}"]
        lines.append(f"•반대 논거·리스크: {esc(x['counter'])}")
        rel = bot.company_line(x.get("companies"))
        secs = ", ".join(x.get("sectors") or [])
        if rel or secs:
            lines.append("관련: " + esc(" / ".join(s for s in (rel, secs) if s)))
        blocks.append("\n\n".join(lines))
    blocks.append("<i>커뮤니티·SNS 반응에서 나온 가설입니다. 판단 전 공시·IR 등 1차 자료로 확인이 필요합니다.</i>")
    send_chunks(head, blocks, False, sep="\n\n━━━━━━━━━━\n")


# ───────────────────────── X(트위터) 트렌드 ─────────────────────────
X_TREND_URL = "https://trends24.in/japan/"
X_TOP_N = int(os.getenv("X_TOP_N", "10"))


def fetch_x_trends(now):
    """trends24 일본 페이지의 시간대별 트렌드(각 50위)를 최근 6시간 동안 합산해 상위 키워드를 고른다.
    점수 = Σ(51 - 순위). 반환: [(키워드, 점수, 등장 시간대 수, 최고 순위)]"""
    h = get(X_TREND_URL).text
    cards = re.findall(r"<h3 class=title data-timestamp=([\d.]+)>.*?</h3><ol class=trend-card__list>(.*?)</ol>", h, re.S)
    if not cards:
        raise RuntimeError("트렌드 카드 0개(페이지 구조 변경 가능성)")
    score, hours, best = {}, {}, {}
    recent = sorted(cards, key=lambda c: -float(c[0]))[:6]  # 최신 시간대 카드 6개(=최근 6시간)
    for ts, body in recent:
        if now - float(ts) > 7 * 3600:
            continue
        for rank, name in enumerate(re.findall(r"class=trend-link>([^<]*)</a>", body), 1):
            name = html.unescape(name).strip()
            score[name] = score.get(name, 0) + 51 - rank
            hours[name] = hours.get(name, 0) + 1
            best[name] = min(best.get(name, 99), rank)
    top = sorted(score, key=lambda k: -score[k])[:X_TOP_N]
    return [(k, score[k], hours[k], best[k]) for k in top]


def news_context(kw):
    """키워드의 최근 1일 구글 뉴스 제목 최대 3개(트렌드 이유 파악용)."""
    try:
        q = urllib.parse.quote(f"{kw.lstrip('#')} when:1d")
        f = feedparser.parse(get(f"https://news.google.com/rss/search?hl=ja&gl=JP&ceid=JP:ja&q={q}").content)
        return [bot.SUFFIX_RE.sub("", html.unescape(e.title)).strip()[:90] for e in f.entries[:3]]
    except Exception:
        return []


X_PROMPT = """너는 한국 자산운용사의 일본 주식 담당 애널리스트를 돕는 데스크다.
아래는 최근 6시간 일본 X(트위터) 트렌드 상위 키워드와, 키워드별 최근 1일 일본 뉴스 제목(있으면)이다.
키워드마다 JSON 배열 항목 하나를 출력하라.

 id
 ko: 키워드의 한국어 표기. 해시태그는 #을 유지. 일본어를 쓰지 말고 고유명사도 한글로(영문은 그대로). 일본 고유명사는 일본어 발음대로 한글 표기하라(예: 第一興商=다이이치코쇼, 한자의 한국식 독음 금지).
 category: 연예·방송, 애니·게임, 스포츠, 사회·사건, 정치·경제, 기업·상품, 기타 중 하나.
 reason: 왜 화제인지 1~2문장(60자 이내), '~함' 보고서체. 뉴스 제목에 근거가 있으면 그것을 쓰고,
         근거가 없으면 키워드로 짐작한 내용을 쓰되 문장 앞에 '(추정)'을 붙여라. 모르면 '(추정) 이유 불명확함'.
 company: 이유가 일본·한국 상장 기업과 직접 관련되면 {{"ko": 한국어 기업명, "official": 일본어 정식 사명, "market": "JP" 또는 "KR"}},
          아니면 빈 객체.
JSON 외 텍스트 금지.

{items}
"""
X_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "STRING"}, "ko": {"type": "STRING"}, "category": {"type": "STRING"}, "reason": {"type": "STRING"},
    "company": {"type": "OBJECT", "properties": {
        "ko": {"type": "STRING"}, "official": {"type": "STRING"}, "market": {"type": "STRING"}}}},
    "required": ["id", "ko", "category", "reason"]}}


def x_trend_pass(state, now_dt, force):
    """다이제스트 회차마다 X 트렌드 상위 10과 화제 이유를 별도 메시지로 보낸다."""
    key, slot_dt = due_slot(now_dt)
    if "last_xslot" not in state and not force:  # 기능 추가 직후: 지난 회차는 건너뛰고 다음 회차부터
        state["last_xslot"] = key
        return None
    if state.get("last_xslot") == key and not force:
        return None
    now = time.time()
    hs = state["health"]["src"]
    try:
        top = fetch_x_trends(now)
        hs["x"] = 0
    except Exception as ex:
        hs["x"] = hs.get("x", 0) + 1
        print(f"[warn] X 트렌드 수집 실패({hs['x']}회 연속): {ex}", file=sys.stderr)
        return None
    if not top:
        state["last_xslot"] = key
        return None
    items = []
    for i, (kw, sc, hrs, best) in enumerate(top):
        items.append(json.dumps({"id": f"x{i}", "keyword": kw, "news": news_context(kw)}, ensure_ascii=False))
        time.sleep(1)
    try:
        res = bot.gemini(state, X_PROMPT.format(items="\n".join(items)), X_SCHEMA,
                         bot.WRITE_MODELS + bot.TRIAGE_MODELS, "X 트렌드")
        by = {r["id"]: r for r in res if isinstance(r, dict) and "id" in r}
        ok = True
    except Exception as ex:
        print(f"[warn] X 트렌드 정리 실패: {ex}", file=sys.stderr)
        since = state.setdefault("x_fail_since", 0) or now
        state["x_fail_since"] = since
        if now - since < FALLBACK_AFTER_H * 3600 and not force:
            return False  # 다음 실행에 재시도
        by, ok = {}, False
    lines = [f"<b>&gt;&gt;[X 트렌드] 일본 최근 6시간 상위 {len(top)}</b> {slot_dt.strftime('%m-%d %H:%M')}"]
    if not ok:
        lines.append("(이유 정리 실패: 키워드와 링크만 보냅니다)")
    for i, (kw, sc, hrs, best) in enumerate(top, 1):
        r = by.get(f"x{i - 1}", {})
        url = "https://x.com/search?q=" + urllib.parse.quote(kw)
        cat = f"[{esc(r['category'])}] " if r.get("category") else ""
        lines.append(f"\n<b>{i}. {cat}{esc(r.get('ko') or kw)}</b> (최고 {best}위 · 6시간 중 {hrs}시간 순위권)")
        if r.get("reason"):
            lines.append(f"• {esc(r['reason'])}")
        co = r.get("company") or {}
        tick = bot.company_line([co]) if co.get("ko") else ""
        lines.append(f"&gt;<a href=\"{esc(url)}\">X에서 보기</a>" + (f" · 관련 기업: {esc(tick)}" if tick else ""))
    if send_chunks(lines[0], lines[1:], slot_dt.hour in SILENT_SLOTS, sep="\n"):
        state["last_xslot"], state["x_fail_since"] = key, 0
        for i in range(len(top)):  # 주간 정리용 기록
            r = by.get(f"x{i}", {})
            if r.get("ko"):
                state["xweek"].append({"ts": now, "ko": r["ko"], "cat": r.get("category", ""),
                                       "reason": r.get("reason", "")})
    return ok


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
            problems[src] = f"{SRC.get(src, {}).get('name', 'X 트렌드(trends24)')} 수집 {n}회 연속 실패(사이트 구조 변경 가능성)"
    alerted = set(h.get("alerted", []))
    new = [k for k in problems if k not in alerted]
    fixed = [k for k in alerted if k not in problems]
    admin = os.getenv("TELEGRAM_ADMIN_CHAT_ID") or None
    if new:
        tg_send("<b>[봇 경고]</b> 일본 커뮤니티봇\n" + "\n".join(f"• {esc(problems[k])}" for k in new)
                + "\n로그: GitHub Actions → jp-community-bot", chat=admin)
    if fixed:
        tg_send("<b>[복구]</b> 일본 커뮤니티봇\n" + "\n".join(
            f"• {'Gemini' if k == 'llm' else SRC.get(k, {}).get('name', 'X 트렌드')} 정상화" for k in fixed), chat=admin)
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
    r2 = digest_pass(state, now_dt, os.getenv("DIGEST_NOW") == "1")
    save_state(state)
    r5 = x_trend_pass(state, now_dt, os.getenv("DIGEST_NOW") == "1" or os.getenv("X_NOW") == "1")
    save_state(state)
    r4 = weekly_pass(state, due_slot(now_dt)[1], os.getenv("WEEKLY_NOW") == "1")
    results = [r for r in (r1, r2, r4, r5) if r is not None]
    check_health(state, all(results) if results else None)
    save_state(state)


if __name__ == "__main__":
    main()
