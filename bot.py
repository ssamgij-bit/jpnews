"""일본 증시 뉴스 → 한국어 번역·인사이트 → 텔레그램 채널 봇.

GitHub Actions에서 10분마다 실행. 상태는 state.json에 저장하고 워크플로우가 커밋한다.
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
MAX_BATCH = 60         # LLM 1회 호출당 최대 기사 수
DRY_RUN = os.getenv("DRY_RUN") == "1"
MODEL_PREF = [m.strip() for m in os.getenv(
    "GEMINI_MODELS",
    "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview,"
    "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]

GN = "https://news.google.com/rss/search?hl=ja&gl=JP&ceid=JP:ja&q="
NHK = "https://news.web.nhk/n-data/conf/na/rss/cat{}.xml"
SOURCES = [
    # (key, 표시명, url, 방식)
    ("nikkei", "니케이", "https://assets.wor.jp/rss/rdf/nikkei/news.rdf", "direct"),
    ("nikkei", "니케이", GN + urllib.parse.quote("site:nikkei.com when:2h"), "gn"),
    ("reuters", "로이터", GN + urllib.parse.quote("site:reuters.com/jp when:2h"), "gn"),
    ("bloomberg", "블룸버그", GN + urllib.parse.quote("site:bloomberg.com/jp when:2h"), "gn"),
    ("nhk", "NHK 경제", NHK.format(5), "direct"),
    ("nhk", "NHK 정치", NHK.format(4), "direct"),
    ("nhk", "NHK 국제", NHK.format(6), "direct"),
    ("nhk", "NHK 사회", NHK.format(1), "direct"),
]
SUFFIX_RE = re.compile(r"(\s*[|｜]\s*ロイター)?\s*[-－]\s*(日本経済新聞|Reuters|ロイター|jp\.reuters\.com|"
                       r"Bloomberg\.com|Bloomberg|NIKKEI Financial|日経ビジネス)\s*$")


# ───────────────────────── 상태 ─────────────────────────
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("seen", {})        # norm_title -> ts
    s.setdefault("recent", [])      # 최근 36h 처리 기사 [{id, ko, ts}] (중복 판정용)
    s.setdefault("digest", [])      # 다이제스트 대기
    s.setdefault("pending", [])     # LLM 실패로 재시도 대기
    s.setdefault("daily", {"date": "", "count": 0})
    s.setdefault("last_digest", "")
    s.setdefault("model", {"name": "", "list": [], "checked": 0})
    return s


def save_state(s):
    now = time.time()
    s["seen"] = {k: v for k, v in s["seen"].items() if now - v < 3 * 86400}
    s["recent"] = [r for r in s["recent"] if now - r["ts"] < 36 * 3600][-400:]
    s["pending"] = s["pending"][-200:]
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


def fetch_sources():
    items, now = [], time.time()
    for key, label, url, mode in SOURCES:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            f = feedparser.parse(r.content)
        except Exception as ex:  # 소스 하나 실패해도 계속
            print(f"[warn] {label} fetch 실패: {ex}", file=sys.stderr)
            continue
        for e in f.entries:
            ts = entry_ts(e)
            if now - ts > MAX_AGE_H * 3600:
                continue
            title = SUFFIX_RE.sub("", html.unescape(e.get("title", ""))).strip()
            if not title:
                continue
            lead = ""
            if mode == "direct":
                lead = re.sub(r"<[^>]+>", "", html.unescape(e.get("summary", "")))[:300]
            items.append({"source": key, "label": label, "title": title, "lead": lead,
                          "link": e.get("link", ""), "gn": mode == "gn", "ts": ts})
        if mode == "gn":
            time.sleep(1.5)  # Google News 과호출 방지
    return items


def dedupe_new(items, state):
    """제목 정규화 + 바이그램 유사도로 1차 중복 제거. 직링크(direct)를 우선."""
    items.sort(key=lambda x: (x["gn"], x["ts"]))
    out, keys = [], []
    recent_keys = list(state["seen"].keys())
    for it in items:
        k = norm(it["title"])
        if k in state["seen"] or any(similar(k, o) for o in keys):
            continue
        if any(similar(k, o) for o in recent_keys[-600:]):
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
    return html.unescape(m.group(1))[:300] if m else ""


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
아래 NEW 기사들(일본어 제목·리드)을 평가해 JSON 배열만 출력하라.

[중요도 기준: 일본 주식 투자자 관점]
5 = 시장 전체를 즉시 움직일 사건: 일본은행 정책 결정·총재의 정책 시사, 환율 개입, 총리 교체·해산·총선 결과,
    대형 경기대책·세제 변경 확정, 미국 관세 등 일본 수출 직격 조치, 시총 상위 기업의 1조엔급 M&A·TOB,
    대형주의 실적 가이던스 대폭 수정, 일본에 직접 파급되는 지정학 쇼크.
4 = 특정 대형주·섹터에 의미 있는 재료: 주요 기업 M&A·대규모 투자·제휴·사업 철수, 주요 지표(GDP·CPI·단칸·임금),
    정책 방향 보도(관계자 발), 금리·엔화에 영향 줄 해외 이벤트, 한일 공급망(반도체·소재·배터리·조선 등) 핵심 뉴스,
    시장 신뢰에 영향 주는 대형 금융사고.
3 = 참고: 중소형주 재료, 업계 동향, 시황 정리 기사, 일반 정치·외교 동향, 해외 거시 지표.
2 = 배경: 투자와 간접적인 사회·국제 뉴스, 칼럼·인터뷰·해설.
1 = 무관: 스포츠, 날씨, 사건사고, 연예, 생활정보.
대부분의 기사는 1~3이다. 4 이상은 하루 10~20건 수준이 되도록 엄격하게 매겨라.
같은 사건의 후속 시황 기사(예: '日経平均 反発' 류 장중 시황)는 3 이하로 둔다.

[중복]
RECENT(이미 처리한 기사)나 NEW 안의 다른 기사와 같은 사건을 다루면 dup_of에 그 id를 넣어라.
NEW 안의 중복은 정보가 가장 많은 기사 하나만 남기고 나머지에 dup_of를 넣는다. 새 사실이 추가된 후속 보도는 중복이 아니다.

[출력 필드]
id, ko_title(자연스러운 한국어 제목), score(1~5 정수), category(시장|거시·정책|기업|정치|국제|사회 중 하나), dup_of(id 또는 null).
score가 4 이상이고 dup_of가 null인 기사만 추가로:
 fact: 기사 제목·리드에 있는 내용만 한국어 1~2문장. 원문에 없는 숫자를 만들지 마라.
 impact: 영향받을 일본 종목·섹터, 관련 있으면 한국 연관 종목·섹터(한일 공급망 포함). 기업은 '영문명(티커 JP/KS)' 형식,
         티커가 확실하지 않으면 티커 없이 기업명만. 1~2문장.
 view: 투자 관점 해석 1~2문장. 반드시 '(추정)'으로 시작하고, 반대 시나리오나 리스크를 한 구절 포함.
JSON 외 텍스트를 출력하지 마라.

RECENT:
{recent}

NEW:
{new}
"""


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
    cands = [p for p in MODEL_PREF if p in names]
    if not cands:  # 목록 조회 실패 시 선호 목록 그대로
        cands = MODEL_PREF[:]
    state["model"] = {"name": cands[0], "list": cands, "checked": time.time()}
    print(f"[info] models = {cands}")
    return cands


SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"}, "ko_title": {"type": "STRING"}, "score": {"type": "INTEGER"},
            "category": {"type": "STRING"}, "dup_of": {"type": "STRING", "nullable": True},
            "fact": {"type": "STRING"}, "impact": {"type": "STRING"}, "view": {"type": "STRING"},
        },
        "required": ["id", "ko_title", "score", "category"],
    },
}


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
    recent = "\n".join(f'{r["id"]}: {r["ko"]}' for r in state["recent"][-150:]) or "(없음)"
    new = "\n".join(json.dumps({"id": it["id"], "src": it["label"], "title": it["title"],
                                "lead": it["lead"]}, ensure_ascii=False) for it in new_items)
    body = {"contents": [{"parts": [{"text": PROMPT.format(recent=recent, new=new)}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 32768,
                                 "responseMimeType": "application/json", "responseSchema": SCHEMA}}
    cands = model_candidates(state, key)
    last = None
    for attempt in range(2):  # 전 모델 혼잡(503)이면 잠시 후 한 바퀴 더
        for m in cands:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}",
                    json=body, timeout=150)
            except Exception as ex:
                last = f"{m}: {ex}"
                continue
            if r.status_code == 200:
                try:
                    cand = r.json()["candidates"][0]
                    txt = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
                    data = parse_json(txt)
                except Exception as ex:
                    last = f"{m}: 응답 파싱 실패 ({ex}), finish={r.json().get('candidates', [{}])[0].get('finishReason')}"
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
    t = datetime.fromtimestamp(it["ts"], JST).strftime("%Y-%m-%d %H:%M")
    return (f"<b>[★{a['score']}] {esc(a.get('ko_title'))}</b>\n"
            f"<i>{esc(it['title'])}</i>\n"
            f"{esc(it['label'])} · {t} JST · {esc(a.get('category', ''))}\n\n"
            f"<b>사실</b> {esc(a.get('fact'))}\n"
            f"<b>영향 종목·섹터</b> {esc(a.get('impact'))}\n"
            f"<b>해석</b> {esc(a.get('view'))}\n\n"
            f"<a href=\"{esc(it['url'])}\">원문 보기</a>")


def send_digest(state, today):
    items = state["digest"]
    if not items:
        state["last_digest"] = today
        return
    order = ["시장", "거시·정책", "기업", "정치", "국제", "사회"]
    items.sort(key=lambda x: (order.index(x["cat"]) if x["cat"] in order else 9, -x["score"], x["ts"]))
    header = f"<b>■ 일본 뉴스 다이제스트 {today}</b> (즉시 알림 제외 {len(items)}건)\n"
    chunks, cur, cat = [], header, None
    for x in items:
        line = ""
        if x["cat"] != cat:
            cat = x["cat"]
            line += f"\n<b>[{esc(cat)}]</b>\n"
        line += f"· <a href=\"{esc(x['url'])}\">{esc(x['ko'])}</a> ({esc(x['label'])})\n"
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
        state["daily"]["date"] = today
        save_state(state)
        return
    batch = state["pending"] + fresh
    state["pending"] = []
    print(f"[info] 신규 {len(fresh)}건, 처리 대상 {len(batch)}건")

    for i in range(0, len(batch), MAX_BATCH):
        chunk = batch[i:i + MAX_BATCH]
        for j, it in enumerate(chunk):
            it["id"] = f"n{int(time.time()) % 100000}_{i + j}"
            if it["source"] == "nikkei" and not it["gn"] and not it["lead"]:
                it["lead"] = og_description(it["link"])
        try:
            res = {a["id"]: a for a in call_llm(state, chunk) if isinstance(a, dict) and "id" in a}
        except Exception as ex:
            print(f"[warn] LLM 처리 실패, 다음 실행에 재시도: {ex}", file=sys.stderr)
            for it in chunk:
                it.setdefault("first_try", time.time())
                if time.time() - it["first_try"] < 3 * 3600:
                    state["pending"].append(it)
                else:  # 3시간 넘게 실패하면 원제로 다이제스트에 넣고 포기
                    state["digest"].append({"ko": it["title"], "url": it["link"], "label": it["label"],
                                            "cat": "미분류", "score": 3, "ts": it["ts"]})
            continue

        for it in sorted(chunk, key=lambda x: x["ts"]):
            a = res.get(it["id"])
            if not a:
                continue
            score = int(a.get("score") or 0)
            ko = a.get("ko_title") or it["title"]
            if a.get("dup_of"):
                continue
            state["recent"].append({"id": it["id"], "ko": ko, "ts": time.time()})
            if score < DIGEST_MIN_SCORE:
                continue
            it["url"] = gn_decode(it["link"]) if it["gn"] else it["link"]
            urgent = score >= THRESHOLD
            if urgent and a.get("fact"):
                if tg_send(fmt_alert(it, a)):
                    state["daily"]["count"] += 1
                continue
            state["digest"].append({"ko": ko, "url": it["url"], "label": it["label"],
                                    "cat": a.get("category", "기타"), "score": score, "ts": it["ts"]})

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
