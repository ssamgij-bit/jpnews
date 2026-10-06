"""일본 관련 뉴스 → 한국어 요약·인사이트 → 텔레그램 채널 봇.

일본 언론(니케이·로이터·블룸버그·NHK)과 중국 언론(월스트리트견문·신랑재경·제일재경)의
일본 관련 기사를 수집한다. GitHub Actions에서 주기 실행, 상태는 state.json(워크플로우가 커밋).
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY
선택: SCORE_THRESHOLD(기본 4), GEMINI_MODELS, DRY_RUN=1
"""
import calendar
import html
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import feedparser
import requests

JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "4"))
DIGEST_MIN_SCORE = 3
DIGEST_HHMM = os.getenv("DIGEST_HHMM", "1545")  # JST, 장 마감(15:30) 이후
MAX_AGE_H = 6          # 이보다 오래된 기사는 무시(첫 실행·지연 대비)
MAX_BATCH = 50         # LLM 1회 호출당 최대 기사 수
RUN_BUDGET = 330       # 초. 이 시간을 넘기면 남은 기사는 다음 실행으로 넘김(워크플로우 타임아웃 방지)
DRY_RUN = os.getenv("DRY_RUN") == "1"
MODEL_PREF = [m.strip() for m in os.getenv(
    "GEMINI_MODELS",
    "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview,"
    "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]
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
    s.setdefault("model", {"name": "", "list": [], "checked": 0})
    if s.get("version", 1) < 2:  # v2: 일본 관련 필터 도입 전 쌓인 다이제스트는 비움
        s["digest"], s["version"] = [], 2
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


def fetch_sources():
    items, now = [], time.time()
    for key, label, url, mode in SOURCES:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            parsed = parse_source(key, label, mode, r, now)
        except Exception as ex:  # 소스 하나 실패해도 계속
            print(f"[warn] {label} fetch 실패: {ex}", file=sys.stderr)
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


# ───────────────────────── LLM ─────────────────────────
PROMPT = """너는 한국 자산운용사의 일본 주식 담당 애널리스트를 돕는 뉴스 데스크다.
NEW 기사들(일본어·중국어 제목과 본문 일부)을 평가해 JSON 배열만 출력하라.

[1. 일본 관련성: japan]
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
japan=false면 score와 무관하게 발송되지 않는다.

[3. 중복: dup_of]
RECENT(이미 처리한 사건)나 NEW 안의 다른 기사와 같은 사건이면 dup_of에 그 id를 넣어라.
같은 사건의 후속 보도라도 새 숫자·공식 발표·결정·당사자 반응 등 실질적으로 새로운 사실이 없으면 중복이다.
새 사실이 있으면 dup_of는 null로 두고 new_info에 무엇이 새로운지 한 구절로 쓴다.
NEW 안에서 같은 사건을 다룬 기사가 여럿이면 정보가 가장 많은 하나만 남기고 나머지는 dup_of로 처리한다.

[4. 출력 필드]
모든 기사: id, japan(true/false), score(1~5 정수), category(시장|거시·정책|기업|정치|국제|사회), dup_of(id 또는 null),
          ko_title(자연스러운 한국어 제목, 기사 핵심을 담은 한 줄), gist(핵심 사실 한 문장, 중복 판정용).
japan=true, score 3 이상, dup_of=null인 기사만 추가로:
 bullets: 한국어 요약 2~3개. 각 항목은 '~함/~했음/~전망' 같은 보고서체 1~2문장.
          기사 제목·본문에 있는 사실·숫자만 쓰고 없는 숫자를 만들지 마라. 열거는 1)…, 2)… 형식.
 insight: 투자 시사점 1~2문장. 반드시 '(추정)'으로 시작. 영향받을 일본 종목·섹터와, 관련 있으면 한국 연관 종목·섹터를 쓰고
          반대 시나리오나 리스크를 한 구절 포함. 기업은 '영문명(티커 JP/KS)' 형식, 티커가 확실하지 않으면 기업명만.
JSON 외 텍스트를 출력하지 마라.

RECENT:
{recent}

NEW:
{new}
"""

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"}, "japan": {"type": "BOOLEAN"}, "score": {"type": "INTEGER"},
            "category": {"type": "STRING"}, "dup_of": {"type": "STRING", "nullable": True},
            "new_info": {"type": "STRING", "nullable": True},
            "ko_title": {"type": "STRING"}, "gist": {"type": "STRING"},
            "bullets": {"type": "ARRAY", "items": {"type": "STRING"}}, "insight": {"type": "STRING"},
        },
        "required": ["id", "japan", "score", "category", "ko_title", "gist"],
    },
}


def model_candidates(state, key):
    """사용 가능한 모델 중 선호 순서대로 후보 목록. 하루 1회 목록 갱신."""
    m = state["model"]
    if m.get("list") and time.time() - m["checked"] < 86400:
        return m["list"]
    try:
        r = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=200",
                         timeout=20)
        names = [x["name"].split("/")[-1] for x in r.json().get("models", [])
                 if "generateContent" in x.get("supportedGenerationMethods", [])]
    except Exception:
        names = []
    cands = [p for p in MODEL_PREF if p in names] or MODEL_PREF[:]
    state["model"] = {"name": cands[0], "list": cands, "checked": time.time()}
    print(f"[info] models = {cands}")
    return cands


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


def call_llm(state, new_items):
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY 없음")
    rec = state["recent"][-200:]
    recent = "\n".join(f'{r["id"]}: {r["ko"]} — {r.get("gist", "")}' for r in rec) or "(없음)"
    new = "\n".join(json.dumps({"id": it["id"], "src": it["label"], "title": it["title"],
                                "body": it["lead"]}, ensure_ascii=False) for it in new_items)
    body = {"contents": [{"parts": [{"text": PROMPT.format(recent=recent, new=new)}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 32768,
                                 "responseMimeType": "application/json", "responseSchema": SCHEMA}}
    cands = model_candidates(state, key)
    last = None
    for attempt in range(2):  # 전 모델 혼잡(503)이면 잠시 후 한 바퀴 더
        for m in cands:
            if time.time() - T0 > RUN_BUDGET:
                raise RuntimeError(f"실행 시간 초과, 남은 기사는 다음 실행으로 ({last})")
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}",
                    json=body, timeout=120)
            except Exception as ex:
                last = f"{m}: {ex}"
                continue
            if r.status_code == 200:
                try:
                    cand = r.json()["candidates"][0]
                    txt = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
                    data = parse_json(txt)
                except Exception as ex:
                    last = f"{m}: 응답 파싱 실패 ({ex})"
                    print(f"[warn] {last}", file=sys.stderr)
                    continue
                print(f"[info] LLM 성공: {m}, {len(data)}건")
                return data
            last = f"{m}: HTTP {r.status_code} {r.text[:150]}"
            print(f"[warn] LLM 실패 {last}", file=sys.stderr)
            if r.status_code == 404:
                state["model"]["checked"] = 0  # 다음 실행 때 모델 목록 재조회
        time.sleep(10)
    raise RuntimeError(last)


# ───────────────────────── 텔레그램 ─────────────────────────
def tg_send(text):
    if DRY_RUN:
        print("──── SEND ────\n" + text)
        return True
    tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
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


def esc(s):
    return html.escape(s or "", quote=False)


def fmt_alert(it, a):
    """하나증권 중국 채널 형식: >>제목 (출처) / •요약 / >원제 링크"""
    t = datetime.fromtimestamp(it["ts"], JST).strftime("%m-%d %H:%M")
    lines = [f"<b>&gt;&gt;{esc(a.get('ko_title'))}</b> ({esc(it['label'])}) ★{a['score']}"]
    if a.get("new_info"):
        lines[0] = lines[0].replace("&gt;&gt;", "&gt;&gt;[후속] ", 1)
    for b in (a.get("bullets") or [])[:3]:
        lines.append(f"\n•{esc(b.lstrip('•· ').strip())}")
    if a.get("insight"):
        lines.append(f"\n•{esc(a['insight'].strip())}")
    lines.append(f"\n&gt;{esc(it['title'])} <a href=\"{esc(it['url'])}\">원문</a> · {t} JST")
    return "\n".join(lines)


def send_digest(state, today):
    items = state["digest"]
    if not items:
        state["last_digest"] = today
        return
    order = ["시장", "거시·정책", "기업", "정치", "국제", "사회"]
    items.sort(key=lambda x: (order.index(x["cat"]) if x["cat"] in order else 9, x["ts"]))
    header = f"<b>&gt;&gt;일본 뉴스 다이제스트 {today}</b> (★3, {len(items)}건)\n"
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
        state["last_digest"] = today


# ───────────────────────── 메인 ─────────────────────────
def main():
    state = load_state()
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    if state["daily"]["date"] != today:
        state["daily"] = {"date": today, "count": 0}

    first_run = not state["seen"]
    fresh = dedupe_new(fetch_sources(), state)
    if first_run:  # 첫 실행은 기존 기사를 '본 것'으로만 기록하고 보내지 않음
        print(f"[info] 첫 실행: {len(fresh)}건을 기준선으로 저장")
        save_state(state)
        return
    batch = state["pending"] + fresh
    state["pending"] = []
    save_state(state)  # 수집 결과를 먼저 저장(도중에 중단돼도 같은 기사를 다시 보내지 않도록)
    print(f"[info] 신규 {len(fresh)}건, 처리 대상 {len(batch)}건")

    for i in range(0, len(batch), MAX_BATCH):
        chunk = batch[i:i + MAX_BATCH]
        if time.time() - T0 > RUN_BUDGET:
            state["pending"].extend(chunk)
            continue
        for j, it in enumerate(chunk):
            it["id"] = f"n{int(time.time()) % 1000000}_{i + j}"
            if it["source"] == "nikkei" and not it["lead"]:
                it["lead"] = og_description(it["link"])
        try:
            res = {a["id"]: a for a in call_llm(state, chunk) if isinstance(a, dict) and "id" in a}
        except Exception as ex:
            print(f"[warn] LLM 처리 실패, 다음 실행에 재시도: {ex}", file=sys.stderr)
            for it in chunk:
                it.setdefault("first_try", time.time())
                if time.time() - it["first_try"] < 3 * 3600:
                    state["pending"].append(it)
            save_state(state)
            continue

        for it in sorted(chunk, key=lambda x: x["ts"]):
            a = res.get(it["id"])
            if not a or a.get("dup_of") or not a.get("japan"):
                continue
            score = int(a.get("score") or 0)
            ko = a.get("ko_title") or it["title"]
            rec = {"id": it["id"], "ko": ko, "gist": a.get("gist", ""), "ts": time.time()}
            state["recent"].append(rec)
            if score < DIGEST_MIN_SCORE:
                continue
            it["url"] = gn_decode(it["link"]) if it["gn"] else it["link"]
            if score >= THRESHOLD and a.get("bullets"):
                if tg_send(fmt_alert(it, a)):
                    state["daily"]["count"] += 1
                    rec["sent"] = True
                    save_state(state)  # 보낸 직후 저장
                continue
            state["digest"].append({"ko": ko, "url": it["url"], "label": it["label"],
                                    "cat": a.get("category", "기타"), "score": score, "ts": it["ts"]})
        save_state(state)

    if now.weekday() < 5 and now.strftime("%H%M") >= DIGEST_HHMM and state["last_digest"] != today:
        send_digest(state, today)

    save_state(state)


if __name__ == "__main__":
    if "--test" in sys.argv:  # 텔레그램 연결 테스트: 상태는 건드리지 않음
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        ok = tg_send(f"<b>[테스트]</b> 일본 뉴스봇 연결 확인 ({now} JST)\n이 메시지가 보이면 채널 발송이 정상입니다.")
        print("[info] 테스트 발송", "성공" if ok else "실패")
        sys.exit(0 if ok else 1)
    main()
