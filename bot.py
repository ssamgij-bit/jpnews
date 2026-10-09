"""일본 관련 뉴스 → 한국어 요약·인사이트 → 텔레그램 채널 봇.

일본 언론(니케이·로이터·블룸버그·NHK)과 중국 언론(월스트리트견문·신랑재경·제일재경)의
일본 관련 기사를 수집한다. GitHub Actions에서 주기 실행, 상태는 state.json(워크플로우가 커밋).
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY
선택: SCORE_THRESHOLD(기본 4), TELEGRAM_ADMIN_CHAT_ID(장애 알림 수신처), GEMINI_TRIAGE_MODELS,
      GEMINI_WRITE_MODELS, DRY_RUN=1
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
from datetime import datetime, timedelta, timezone

import feedparser
import requests

JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "4"))
DIGEST_MIN_SCORE = THRESHOLD  # ★3 이하는 보내지 않음
DIGEST_SLOTS = []  # 다이제스트·아침 브리핑 사용 안 함(★4 이상 즉시 알림만)
FAIL_ALERT_N = 3       # 같은 실패가 연속 이 횟수에 이르면 경고 발송
MAX_AGE_H = 6          # 이보다 오래된 기사는 무시(첫 실행·지연 대비)
MAX_BATCH = 50         # LLM 1회 호출당 최대 기사 수
RUN_BUDGET = 330       # 초. 이 시간을 넘기면 남은 기사는 다음 실행으로 넘김(워크플로우 타임아웃 방지)
DRY_RUN = os.getenv("DRY_RUN") == "1"
def _models(env, default):
    return [m.strip() for m in os.getenv(env, default).split(",") if m.strip()]


# 1단계(선별: 일본 관련·점수·중복)는 경량 모델, 2단계(★4 이상 요약)는 상위 모델
TRIAGE_MODELS = _models("GEMINI_TRIAGE_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite")
WRITE_MODELS = _models("GEMINI_WRITE_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,"
                                              "gemini-3-flash-preview,gemini-2.5-flash")
T0 = time.time()

GN = "https://news.google.com/rss/search?hl=ja&gl=JP&ceid=JP:ja&q="
NHK = "https://news.web.nhk/n-data/conf/na/rss/cat{}.xml"
WOR = "https://assets.wor.jp/rss/rdf/nikkei/{}.rdf"
SOURCES = [
    # (key, 표시명, url, 방식) — 니케이는 기사(DGXZQO)만, 보도자료·공시 페이지는 제외
    ("nikkei", "니케이", WOR.format("news"), "rss"),
    ("nikkei", "니케이", WOR.format("markets"), "rss"),
    ("nikkei", "니케이", WOR.format("economy"), "rss"),
    ("nikkei", "니케이", WOR.format("business"), "rss"),
    ("nikkei", "니케이", WOR.format("international"), "rss"),
    ("reuters", "로이터", GN + urllib.parse.quote("site:reuters.com/jp when:2h"), "gn"),
    ("bloomberg", "블룸버그", GN + urllib.parse.quote("site:bloomberg.com/jp when:2h"), "gn"),
    ("nhk", "NHK", NHK.format(5), "rss"),
    ("nhk", "NHK", NHK.format(4), "rss"),
    ("nhk", "NHK", NHK.format(6), "rss"),
    ("nhk", "NHK", NHK.format(1), "rss"),
    ("wscn", "월스트리트견문", "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=100", "wscn_live"),
    ("wscn", "월스트리트견문", "https://api-one-wscn.awtmt.com/apiv1/content/information-flow?channel=global-channel&accept=article&limit=100", "wscn_art"),
    ("sina", "신랑재경", "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=100&zhibo_id=152", "sina"),
    ("yicai", "제일재경", "https://www.yicai.com/api/ajax/getlatest?page=1&pagesize=50", "yicai"),
]
CN_SOURCES = {"wscn", "sina", "yicai"}
# 중국 언론은 일본 관련 키워드가 있는 기사만 받는다
JP_KW = re.compile(r"日本|日元|日圆|日经|日經|东京|東京|大阪|日银|日本央行|植田|高市|石破|岸田|日企|日股|日债|日本国债|"
                   r"中日|日中|日美|美日|日韩|韩日|丰田|索尼|软银|任天堂|三菱|三井|住友|瑞穗|野村|本田|日产|松下|"
                   r"东京电子|爱德万|铠侠|台积电熊本|日立|富士|佳能|尼康|优衣库|迅销|7-11|7-Eleven|冲绳|钓鱼岛|"
                   r"日方|日媒|日本政府|日本首相|日本经济|日本企业")
SUFFIX_RE = re.compile(r"(\s*[|｜]\s*ロイター)?\s*[-－]\s*(日本経済新聞|Reuters|ロイター|jp\.reuters\.com|"
                       r"Bloomberg\.com|Bloomberg|NIKKEI Financial|日経ビジネス|nikkei\.com)\s*$")


# ───────────────────────── 상태 ─────────────────────────
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("seen", {})        # norm_title -> ts
    s.setdefault("recent", [])      # 최근 48h 처리 사건 [{id, ko, gist, ts, sent}] (중복 판정용)
    s.setdefault("digest", [])      # 다이제스트 대기
    s.setdefault("pending", [])     # LLM 실패·시간 초과로 재시도 대기
    s.setdefault("daily", {"date": "", "count": 0})
    s.setdefault("last_digest", "")
    s.setdefault("model", {"names": [], "checked": 0})
    s.setdefault("health", {"llm": 0, "src": {}, "alerted": []})
    if s.get("version", 1) < 3:  # v3: 다이제스트 폐지, 쌓인 항목 비움
        s["digest"], s["version"] = [], 3
    return s


def save_state(s):
    now = time.time()
    s["seen"] = {k: v for k, v in s["seen"].items() if now - v < 3 * 86400}
    s["recent"] = [r for r in s["recent"] if now - r["ts"] < 48 * 3600][-500:]
    s["pending"] = s["pending"][-200:]
    s["digest"] = s["digest"][-300:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=0)


# ───────────────────────── 수집 ─────────────────────────
def norm(t):
    t = SUFFIX_RE.sub("", t)
    t = re.sub(r"[\s　、。・「」『』（）()【】\[\]!！?？:：,，.．\-－―─~〜｜|＝=\"'“”]", "", t)
    return t.lower()


def bigrams(t):
    return {t[i:i + 2] for i in range(len(t) - 1)} or {t}


def similar(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / max(1, min(len(A), len(B))) >= 0.7


def entry_ts(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return calendar.timegm(t) if t else time.time()


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def cn_split(text):
    """중국 속보 '【제목】본문' 형식 분리."""
    m = re.match(r"\s*【([^】]{4,80})】(.*)", text, re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text[:60].strip(), text.strip()


def parse_source(key, label, mode, r, now):
    out = []
    if mode in ("rss", "gn"):
        for e in feedparser.parse(r.content).entries:
            link = e.get("link", "")
            if key == "nikkei" and "/article/DGXZQO" not in link:
                continue  # 보도자료(DGXZRSP)·공시 등 제외
            title = SUFFIX_RE.sub("", html.unescape(e.get("title", ""))).strip()
            lead = strip_tags(e.get("summary", ""))[:400] if mode == "rss" else ""
            out.append({"title": title, "lead": lead, "link": link, "ts": entry_ts(e)})
    elif mode == "wscn_live":
        for x in r.json()["data"]["items"]:
            body = strip_tags(x.get("content_text") or x.get("content", ""))
            title = (x.get("title") or "").strip()
            if not title:
                title, body = cn_split(body)
            out.append({"title": title, "lead": body[:600], "link": x.get("uri", ""), "ts": x["display_time"]})
    elif mode == "wscn_art":
        for it in r.json()["data"]["items"]:
            a = it.get("resource") or {}
            if it.get("resource_type") != "article" or not a.get("title"):
                continue
            out.append({"title": a["title"], "lead": strip_tags(a.get("content_short", ""))[:600],
                        "link": a.get("uri", ""), "ts": a.get("display_time", now)})
    elif mode == "sina":
        for x in r.json()["result"]["data"]["feed"]["list"]:
            title, body = cn_split(strip_tags(x.get("rich_text", "")))
            ts = datetime.strptime(x["create_time"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(hours=8))).timestamp()
            out.append({"title": title, "lead": body[:600],
                        "link": f"https://finance.sina.com.cn/7x24/?id={x['id']}", "ts": ts})
    elif mode == "yicai":
        for x in r.json():
            ts = datetime.fromisoformat(x["CreateDate"]).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
            out.append({"title": x.get("NewsTitle", ""), "lead": strip_tags(x.get("NewsNotes", ""))[:600],
                        "link": "https://www.yicai.com" + x.get("url", ""), "ts": ts})
    if key in CN_SOURCES:
        out = [o for o in out if JP_KW.search(o["title"] + " " + o["lead"])]
    return out


def fetch_sources(state):
    items, now = [], time.time()
    src = state["health"]["src"]
    for key, label, url, mode in SOURCES:
        sid = f"{label}|{url.split('?')[0][-40:]}"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            parsed = parse_source(key, label, mode, r, now)
            src[sid] = 0
        except Exception as ex:  # 소스 하나 실패해도 계속
            src[sid] = src.get(sid, 0) + 1
            print(f"[warn] {label} fetch 실패({src[sid]}회 연속): {ex}", file=sys.stderr)
            continue
        for p in parsed:
            if not p["title"] or now - p["ts"] > MAX_AGE_H * 3600:
                continue
            items.append({"source": key, "label": label, "gn": mode == "gn", **p})
        if mode == "gn":
            time.sleep(1.5)  # Google News 과호출 방지
    return items


def dedupe_new(items, state):
    """제목 정규화 + 바이그램 유사도로 1차 중복 제거. 직링크를 우선."""
    items.sort(key=lambda x: (x["gn"], x["ts"]))
    out, keys = [], []
    recent_keys = list(state["seen"].keys())[-800:]
    for it in items:
        k = norm(it["title"])
        if k in state["seen"] or any(similar(k, o) for o in keys):
            continue
        if any(similar(k, o) for o in recent_keys):
            state["seen"][k] = time.time()
            continue
        keys.append(k)
        state["seen"][k] = time.time()
        it["key"] = k
        out.append(it)
    return out


def og_description(url):
    try:
        h = requests.get(url, headers=UA, timeout=12).text
    except Exception:
        return ""
    m = (re.search(r'<meta[^>]+(?:property|name)="og:description"[^>]+content="([^"]*)"', h)
         or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="og:description"', h))
    return html.unescape(m.group(1))[:400] if m else ""


def gn_decode(link):
    """Google News RSS 링크 → 원문 URL. 실패 시 원래 링크 반환."""
    try:
        gid = urllib.parse.urlparse(link).path.split("/")[-1]
        h = requests.get(f"https://news.google.com/rss/articles/{gid}", headers=UA, timeout=15).text
        sg = re.search(r'data-n-a-sg="([^"]+)"', h)
        ts = re.search(r'data-n-a-ts="([^"]+)"', h)
        if not (sg and ts):
            return link
        req = [["Fbv4je", f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,'
                          f'null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{gid}",{ts.group(1)},'
                          f'"{sg.group(1)}"]']]
        r = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                          headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                          data="f.req=" + urllib.parse.quote(json.dumps([req])), timeout=15)
        body = r.text.split("\n\n", 1)[1]
        url = json.loads(json.loads(body)[0][2])[1]
        return url if url.startswith("http") else link
    except Exception:
        return link


# ───────────────────────── 티커 대조표 ─────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CO_SUFFIX = re.compile(r"(ホールディングス|ＨＤ|HD|グループ|株式会社|\(株\)|（株）|홀딩스|지주|\s)", re.I)


def _cnorm(s):
    return _CO_SUFFIX.sub("", unicodedata.normalize("NFKC", s or "")).lower()


def _load_list(fname, has_suffix):
    """{'exact': 정식명→종목, 'loose': 접미어 제거명→[종목들]}"""
    path = os.path.join(DATA_DIR, fname)
    exact, loose = {}, {}
    try:
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                v = (row["code"], row["name"], row.get("suffix") if has_suffix else "JP")
                exact.setdefault(unicodedata.normalize("NFKC", row["name"]).lower().replace(" ", ""), v)
                lst = loose.setdefault(_cnorm(row["name"]), [])
                if v not in lst:
                    lst.append(v)
    except FileNotFoundError:
        print(f"[warn] {fname} 없음: 티커 표시 생략", file=sys.stderr)
    return {"exact": exact, "loose": loose}


JPX = _load_list("jpx_list.csv", False)
KRX = _load_list("krx_list.csv", True)


def lookup_ticker(name, table):
    """상장사 목록에서만 티커를 찾는다. 정식명 일치 → 접미어 제거 후 유일 일치.
    (앞부분만 비슷한 이름으로 맞추던 방식은 다른 회사로 잘못 연결돼 폐지)"""
    raw = unicodedata.normalize("NFKC", name or "").lower().replace(" ", "")
    if len(raw) < 2:
        return None
    if raw in table["exact"]:
        return table["exact"][raw]
    q = _cnorm(name)
    hits = table["loose"].get(q, [])
    if len(hits) == 1:
        return hits[0]
    return None  # 여러 종목과 겹치거나 목록에 없으면 표시하지 않음


def company_line(companies):
    out, seen = [], set()
    for c in companies or []:
        kr = (c.get("market") or "").upper() == "KR"
        table = KRX if kr else JPX
        hit = lookup_ticker(c.get("official") or "", table)
        if not hit and kr:  # 한국 기업은 한국어 이름으로도 대조
            hit = lookup_ticker(c.get("ko") or "", table)
        code = (c.get("code") or "").strip().upper()
        if hit and code and code != hit[0]:
            hit = None  # 모델이 준 종목코드와 사명이 서로 맞지 않으면 버림
        if not hit or hit[0] in seen:
            continue  # 목록에서 확인되지 않은 기업은 표시하지 않음
        seen.add(hit[0])
        out.append(f"{c.get('ko') or hit[1]}({hit[0]} {hit[2]})")
    return ", ".join(out[:6])


# ───────────────────────── LLM ─────────────────────────
RULES = """[1. 일본 관련성: japan]
true = 일본 기업·일본 주식/채권/엔화 시장·일본 정부/일본은행/일본 정치·일본 경제지표,
       또는 일본을 명시적으로 겨냥하거나 일본에 직접 영향이 명시된 해외 조치(예: 미국의 대일 관세, 중국의 대일 수출 규제, 중일 관계).
false = 일본이 언급되지 않거나 주변적으로만 언급된 해외 뉴스(미국 증시·유럽 정치·중동·브라질 선거 등),
        일본 기업이 나오지 않는 해외 기업 뉴스. 일본 매체가 보도했다는 사실만으로는 true가 아니다.

[2. 중요도: score, 일본 주식 투자자 관점]
5 = 시장 전체를 즉시 움직일 사건: 일본은행 정책 결정·총재의 정책 시사, 환율 개입, 총리 교체·해산·총선 결과,
    대형 경기대책·세제 변경 확정, 일본 수출을 직격하는 관세·규제, 시총 상위 기업의 1조엔급 M&A·TOB,
    대형주의 실적 가이던스 대폭 수정, 일본에 직접 파급되는 지정학 쇼크.
4 = 특정 대형주·섹터 주가에 의미 있는 재료: 주요 기업의 M&A·대규모 투자·제휴·사업 철수·실적 서프라이즈,
    주요 지표(GDP·CPI·단칸·임금) 발표, 정책 방향 보도(관계자 발), 한일 공급망(반도체·소재·배터리·조선) 핵심 뉴스,
    시장 신뢰에 영향 주는 대형 금융사고.
3 = 참고: 중소형주 재료, 업계 동향, 일반 정치·외교 동향, 해외 언론의 일본 분석.
2 = 배경: 칼럼·인터뷰·해설, 지역 뉴스, 장중 시황(지수 등락·환율·채권 시세 틱), 주가 등락만 전하는 기사.
1 = 무관: 스포츠, 날씨, 사건사고, 연예, 생활정보, 보도자료·신제품 홍보, 공시 목록.
대부분의 기사는 1~3이다. 4 이상은 전체의 3% 안팎, 하루 10~20건이 되도록 엄격하게 매겨라.

[3. 중복: dup_of]
RECENT(이미 처리한 사건)나 NEW 안의 다른 기사와 같은 사건이면 dup_of에 그 id를 넣어라.
같은 사건의 후속 보도라도 새 숫자·공식 발표·결정·당사자 반응 등 실질적으로 새로운 사실이 없으면 중복이다.
새 사실이 있으면 dup_of는 null로 두고 new_info에 무엇이 새로운지 한 구절로 쓴다.
NEW 안에서 같은 사건을 다룬 기사가 여럿이면 정보가 가장 많은 하나만 남기고 나머지는 dup_of로 처리한다."""

TRIAGE_PROMPT = """너는 한국 자산운용사의 일본 주식 담당 애널리스트를 돕는 뉴스 데스크다.
NEW 기사들(일본어·중국어 제목과 본문 일부)을 선별해 JSON 배열만 출력하라.

""" + RULES + """

[4. 출력 필드] 기사마다: id, japan(true/false), score(1~5 정수), category(시장|거시·정책|기업|정치|국제|사회),
dup_of(id 또는 null), new_info(문자열 또는 null), ko_title(정확한 한국어 제목 한 줄), gist(핵심 사실 한 문장).
고유명사(지명·기관·인물)는 원문대로 정확히 옮겨라(예: 駐日米軍=주일미군). JSON 외 텍스트를 출력하지 마라.

RECENT:
{recent}

NEW:
{new}
"""

WRITE_PROMPT = """너는 한국 자산운용사의 일본 주식 담당 애널리스트를 돕는 뉴스 데스크다.
아래 중요 기사들(일본어·중국어)을 한국어로 정리해 JSON 배열만 출력하라.

기사마다 출력:
 id
 ko_title: 기사 핵심을 담은 정확한 한국어 제목 한 줄. 고유명사는 원문대로 정확히(예: 駐日米軍=주일미군).
 bullets: 요약 2~3개. 각 항목은 '~함/~했음/~전망' 같은 보고서체 1~2문장.
          기사 제목·본문에 있는 사실·숫자만 쓰고, 없는 숫자를 만들지 마라. 열거는 1)…, 2)… 형식.
 insight: 투자 시사점 1~2문장. 반드시 '(추정)'으로 시작한다. 종목명·티커를 나열하지 말고,
          이 뉴스가 시장·업종·투자 판단에 주는 의미를 쓴다. 반대 시나리오나 리스크를 한 구절 포함한다.
 companies: 기사에 직접 등장하는 상장 기업 목록(최대 6개). 각 항목은
          {{"ko": 한국어 통용 기업명, "official": 상장 정식 사명(일본 기업은 일본어 정식 사명, 한국 기업은 한국어 정식 사명),
            "code": 종목코드(일본 4자리·한국 6자리, 모르면 빈 문자열), "market": "JP" 또는 "KR"}}.
          일본·한국 기업만, 확실하지 않으면 넣지 마라. 비슷한 이름의 다른 회사를 넣지 마라.
JSON 외 텍스트를 출력하지 마라.

기사:
{items}
"""

TRIAGE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"}, "japan": {"type": "BOOLEAN"}, "score": {"type": "INTEGER"},
            "category": {"type": "STRING"}, "dup_of": {"type": "STRING", "nullable": True},
            "new_info": {"type": "STRING", "nullable": True},
            "ko_title": {"type": "STRING"}, "gist": {"type": "STRING"},
        },
        "required": ["id", "japan", "score", "category", "ko_title", "gist"],
    },
}
WRITE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"}, "ko_title": {"type": "STRING"},
            "bullets": {"type": "ARRAY", "items": {"type": "STRING"}}, "insight": {"type": "STRING"},
            "companies": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "ko": {"type": "STRING"}, "official": {"type": "STRING"}, "code": {"type": "STRING"},
                "market": {"type": "STRING"}}}},
        },
        "required": ["id", "ko_title", "bullets", "insight"],
    },
}


def available_models(state, key):
    """API 키로 쓸 수 있는 모델 목록. 하루 1회 갱신."""
    m = state["model"]
    if m.get("names") and time.time() - m.get("checked", 0) < 86400:
        return m["names"]
    try:
        r = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=200",
                         timeout=20)
        names = [x["name"].split("/")[-1] for x in r.json().get("models", [])
                 if "generateContent" in x.get("supportedGenerationMethods", [])]
    except Exception:
        names = []
    m.update({"names": names, "checked": time.time() if names else 0})  # dead(퇴역 모델) 기록은 유지
    return names


def parse_json(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        a, b = txt.find("["), txt.rfind("]")
        if a < 0 or b <= a:
            raise
        data = json.loads(re.sub(r",\s*([}\]])", r"\1", txt[a:b + 1]))
    if isinstance(data, dict):  # {"items":[...]} 형태로 올 때
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return data


BUSY = set()  # 이번 실행에서 혼잡(503·429)이었던 모델: 같은 실행의 다음 호출에서는 뒤로 미룬다


def _order(state, prefs, names):
    """사용 가능 + 퇴역(404) 아님, 이번 실행에서 혼잡했던 모델은 맨 뒤."""
    m = state["model"]
    dead = {k: v for k, v in m.get("dead", {}).items() if time.time() - v < 7 * 86400}
    m["dead"] = dead
    cands = [p for p in prefs if (not names or p in names) and p not in dead] or \
            [p for p in prefs if p not in dead] or list(prefs)
    cands.sort(key=lambda p: p in BUSY)  # 우선순위는 유지(상위 모델 먼저), 이번 실행 혼잡 모델만 뒤로
    return cands


def _call(key, m, body):
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}",
                      json=body, timeout=75)
    if r.status_code != 200:
        return r.status_code, None, f"HTTP {r.status_code} {r.text[:120]}"
    cand = (r.json().get("candidates") or [{}])[0]
    txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
    try:
        return 200, parse_json(txt), None
    except Exception as ex:
        return 200, None, f"응답 파싱 실패 ({ex}; finish={cand.get('finishReason')}, 길이={len(txt)})"


def gemini(state, prompt, schema, prefs, tag):
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY 없음")
    names = available_models(state, key)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 32768,
                                 "responseMimeType": "application/json", "responseSchema": schema}}
    last = None
    for attempt in range(2):  # 전 모델 혼잡이면 잠시 후 한 바퀴 더
        for m in _order(state, prefs, names):
            if time.time() - T0 > RUN_BUDGET:
                raise RuntimeError(f"실행 시간 초과 ({last})")
            for retry in range(2):  # 응답 파싱 실패는 같은 모델로 1회 재시도
                try:
                    code, data, err = _call(key, m, body)
                except Exception as ex:
                    code, data, err = 0, None, str(ex)
                if data is not None:
                    print(f"[info] [{tag}] {m}: {len(data)}건")
                    BUSY.discard(m)
                    return data
                last = f"{m}: {err}"
                print(f"[warn] [{tag}] {last}", file=sys.stderr)
                if code != 200:
                    break
            if code in (503, 429):
                BUSY.add(m)
            elif code == 404:  # 퇴역 모델: 7일간 제외하고 다음 실행 때 목록 재조회
                state["model"].setdefault("dead", {})[m] = time.time()
                state["model"]["checked"] = 0
        if attempt == 0:
            time.sleep(5)
    raise RuntimeError(last)


def triage(state, items):
    rec = state["recent"][-200:]
    recent = "\n".join(f'{r["id"]}: {r["ko"]} — {r.get("gist", "")}' for r in rec) or "(없음)"
    new = "\n".join(json.dumps({"id": it["id"], "src": it["label"], "title": it["title"], "body": it["lead"]},
                               ensure_ascii=False) for it in items)
    return gemini(state, TRIAGE_PROMPT.format(recent=recent, new=new), TRIAGE_SCHEMA, TRIAGE_MODELS, "선별")


def write_up(state, items):
    body = "\n".join(json.dumps({"id": it["id"], "src": it["label"], "title": it["title"], "body": it["lead"]},
                                ensure_ascii=False) for it in items)
    # 상위 모델이 모두 실패하면 경량 모델로라도 작성
    return gemini(state, WRITE_PROMPT.format(items=body), WRITE_SCHEMA, WRITE_MODELS + TRIAGE_MODELS, "요약")


# ───────────────────────── 텔레그램 ─────────────────────────
def tg_send(text, chat=None):
    if DRY_RUN:
        print("──── SEND ────\n" + text)
        return True
    tok = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = chat or os.environ["TELEGRAM_CHAT_ID"]
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20)
        if r.status_code == 200:
            time.sleep(1.1)
            return True
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        print(f"[warn] telegram {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    return False


def admin_send(text):
    return tg_send(text, os.getenv("TELEGRAM_ADMIN_CHAT_ID") or None)


def esc(s):
    return html.escape(s or "", quote=False)


def fmt_alert(it, tri, w):
    """하나증권 중국 채널 형식: >>제목 (출처) / •요약 / •(추정) 시사점 / >원제 링크"""
    t = datetime.fromtimestamp(it["ts"], JST).strftime("%m-%d %H:%M")
    head = "[후속] " if tri.get("new_info") else ""
    lines = [f"<b>&gt;&gt;{head}{esc(w.get('ko_title') or tri.get('ko_title'))}</b> ({esc(it['label'])}) ★{tri['score']}"]
    for b in (w.get("bullets") or [])[:3]:
        lines.append(f"\n•{esc(b.lstrip('•· ').strip())}")
    if w.get("insight"):
        lines.append(f"\n•{esc(w['insight'].strip())}")
    cos = company_line(w.get("companies"))
    if cos:
        lines.append(f"\n언급 기업: {esc(cos)}")
    lines.append(f"\n&gt;{esc(it['title'])} <a href=\"{esc(it['url'])}\">원문</a> · {t} JST")
    return "\n".join(lines)


def send_digest(state, slot_key, title):
    items = state["digest"]
    if not items:
        state["last_digest"] = slot_key
        return
    order = ["시장", "거시·정책", "기업", "정치", "국제", "사회"]
    items.sort(key=lambda x: (order.index(x["cat"]) if x["cat"] in order else 9, x["ts"]))
    header = f"<b>&gt;&gt;{esc(title)}</b> {slot_key[:10]} (★3, {len(items)}건)\n"
    chunks, cur, cat = [], header, None
    for x in items:
        line = ""
        if x["cat"] != cat:
            cat = x["cat"]
            line += f"\n<b>[{esc(cat)}]</b>\n"
        line += f"•<a href=\"{esc(x['url'])}\">{esc(x['ko'])}</a> ({esc(x['label'])})\n"
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += line
    chunks.append(cur)
    if all(tg_send(c) for c in chunks):
        state["digest"] = []
        state["last_digest"] = slot_key


def check_health(state, llm_ok):
    """연속 실패 감지 → 경고 1회, 회복 시 1회 알림."""
    h = state["health"]
    if llm_ok is not None:
        h["llm"] = 0 if llm_ok else h.get("llm", 0) + 1
    problems = {}
    if h.get("llm", 0) >= FAIL_ALERT_N:
        problems["llm"] = f"Gemini 처리 {h['llm']}회 연속 실패(기사는 보관 중, 3시간 넘으면 폐기)"
    for sid, n in h["src"].items():
        if n >= FAIL_ALERT_N:
            problems[sid] = f"소스 수집 {n}회 연속 실패: {sid.split('|')[0]}"
    alerted = set(h.get("alerted", []))
    new = [k for k in problems if k not in alerted]
    fixed = [k for k in alerted if k not in problems]
    if new:
        admin_send("<b>[봇 경고]</b> 일본 뉴스봇\n" + "\n".join(f"• {esc(problems[k])}" for k in new)
                   + "\n로그: GitHub Actions → jp-news-bot")
    if fixed:
        admin_send("<b>[복구]</b> 일본 뉴스봇\n" + "\n".join(f"• {'Gemini' if k == 'llm' else esc(k.split('|')[0])} 정상화" for k in fixed))
    h["alerted"] = list(problems)


# ───────────────────────── 메인 ─────────────────────────
def main():
    state = load_state()
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    if state["daily"]["date"] != today:
        state["daily"] = {"date": today, "count": 0}

    first_run = not state["seen"]
    fresh = dedupe_new(fetch_sources(state), state)
    if first_run:  # 첫 실행은 기존 기사를 '본 것'으로만 기록하고 보내지 않음
        print(f"[info] 첫 실행: {len(fresh)}건을 기준선으로 저장")
        save_state(state)
        return
    batch = state["pending"] + fresh
    state["pending"] = []
    save_state(state)  # 수집 결과를 먼저 저장(도중에 중단돼도 같은 기사를 다시 보내지 않도록)
    print(f"[info] 신규 {len(fresh)}건, 처리 대상 {len(batch)}건")

    llm_ok = None
    for i in range(0, len(batch), MAX_BATCH):
        chunk = batch[i:i + MAX_BATCH]
        if time.time() - T0 > RUN_BUDGET:
            state["pending"].extend(chunk)
            continue
        for j, it in enumerate(chunk):
            it["id"] = f"n{int(time.time()) % 1000000}_{i + j}"
            if it["source"] == "nikkei" and not it["lead"]:
                it["lead"] = og_description(it["link"])
        try:  # 1단계: 선별
            res = {a["id"]: a for a in triage(state, chunk) if isinstance(a, dict) and "id" in a}
            llm_ok = True if llm_ok is None else llm_ok
        except Exception as ex:
            llm_ok = False
            print(f"[warn] 선별 실패, 다음 실행에 재시도: {ex}", file=sys.stderr)
            for it in chunk:
                it.setdefault("first_try", time.time())
                if time.time() - it["first_try"] < 3 * 3600:
                    state["pending"].append(it)
            save_state(state)
            continue

        urgent = []
        for it in sorted(chunk, key=lambda x: x["ts"]):
            a = res.get(it["id"])
            if not a or a.get("dup_of") or not a.get("japan"):
                continue
            score = int(a.get("score") or 0)
            rec = {"id": it["id"], "ko": a.get("ko_title") or it["title"], "gist": a.get("gist", ""), "ts": time.time()}
            state["recent"].append(rec)
            if score < DIGEST_MIN_SCORE:
                continue
            it["url"] = gn_decode(it["link"]) if it["gn"] else it["link"]
            if score >= THRESHOLD:
                urgent.append((it, a, rec))
            else:
                state["digest"].append({"ko": rec["ko"], "url": it["url"], "label": it["label"],
                                        "cat": a.get("category", "기타"), "score": score, "ts": it["ts"]})
        save_state(state)

        if urgent:  # 2단계: ★4 이상만 상위 모델로 요약
            try:
                ws = {w["id"]: w for w in write_up(state, [u[0] for u in urgent]) if isinstance(w, dict) and "id" in w}
            except Exception as ex:
                print(f"[warn] 요약 실패: {ex}", file=sys.stderr)
                ws = {}
            for it, a, rec in urgent:
                w = ws.get(it["id"])
                if not w or not w.get("bullets"):  # 요약을 못 만들면 제목·링크만 보냄
                    w = {"ko_title": rec["ko"], "bullets": [], "insight": ""}
                if tg_send(fmt_alert(it, a, w)):
                    state["daily"]["count"] += 1
                    rec["sent"] = True
                    if w.get("ko_title"):
                        rec["ko"] = w["ko_title"]
                    save_state(state)  # 보낸 직후 저장

    # 다이제스트: 평일 08:30(밤사이)·15:45(장중). 가장 최근에 지난 시각 기준으로 한 번씩
    if now.weekday() < 5:
        due = [(s, t) for s, t in DIGEST_SLOTS if now.strftime("%H%M") >= s]
        if due:
            s, t = due[-1]
            key = f"{today} {s}"
            if state["last_digest"] != key:
                send_digest(state, key, t)

    check_health(state, llm_ok)
    save_state(state)


if __name__ == "__main__":
    if "--test" in sys.argv:  # 텔레그램 연결 테스트: 상태는 건드리지 않음
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        ok = tg_send(f"<b>[테스트]</b> 일본 뉴스봇 연결 확인 ({now} JST)\n이 메시지가 보이면 채널 발송이 정상입니다.")
        print("[info] 테스트 발송", "성공" if ok else "실패")
        sys.exit(0 if ok else 1)
    main()
