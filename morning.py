"""평일 08:30(KST) 아침 브리핑 PDF → 텔레그램.

구성
 1) 글로벌 시황(미국 중심): 지수·금리·환율·원자재·섹터 ETF·대형주 등락(Yahoo), S&P500 상승·하락 상위(finviz),
    참고 텔레그램 채널(사제콩이 @ehdwl, 미국 주식 인사이더 @insidertracking)의 아침 글 → AI 정리
 2) 일본 증시 시황: 지수(Yahoo), 신고가 봇의 마감 정리(highs_state.json), 뉴스봇이 보낸 기사(state.json),
    공시봇 하이라이트(jpall 저장소 state 브랜치) → AI 정리
 3) 커뮤니티 화제글·X 트렌드·note 급상승 주제·주간 순위(community_state.json의 pdfq — 텔레그램 대신 여기로 모임)
 4) 일본 52주 신고가·상승률 상위(highs_state.json의 pdfq)
지난 PDF 이후 쌓인 것을 모두 묶는다(월요일 PDF = 금요일 아침 이후 주말분 포함).

환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY
선택: MORNING_NOW=1(시각·중복 확인 없이 지금 만들어 보냄), DRY_RUN=1(보내지 않고 morning.pdf만 저장)
실행: python morning.py          → 08:30이 지났고 오늘 아직 안 보냈으면(11시 전) 만들어 보냄
      python morning.py --wait   → 만들고 08:30까지 기다렸다가 보냄(이어 달리기에서 08:10에 시작)
"""
import base64
import glob
import html as H
import importlib.util
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import requests

import bot
from bot import JST, UA, esc

bot.RUN_BUDGET = 3000  # AI 호출 시간 예산(초)
DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(DIR, "morning_state.json")
DRY_RUN = os.getenv("DRY_RUN") == "1"
FORCE = os.getenv("MORNING_NOW") == "1"
SEND_AT = (8, 30)
LATE_H = 11
CHANNELS = [("ehdwl", "사제콩이(서상영)"), ("insidertracking", "미국 주식 인사이더")]
JPALL_STATE = "https://raw.githubusercontent.com/ssamgij-bit/jpall/state/state.json"
MODELS = bot.WRITE_MODELS + bot.TRIAGE_MODELS


def now():
    return datetime.now(JST)


def load_state():
    try:
        s = json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        s = {}
    s.setdefault("model", {"names": [], "checked": 0})
    return s


def save_state(s):
    json.dump(s, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


def gemini_obj(state, prompt, schema, tag):
    """객체 JSON 응답(AI). 실패하면 None"""
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        return None
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 16000,
                                 "responseMimeType": "application/json", "responseSchema": schema}}
    names = bot.available_models(state, key)
    for attempt in range(2):
        for m in bot._order(state, MODELS, names):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}",
                                  json=body, timeout=180)
                if r.status_code != 200:
                    print(f"[warn] [{tag}] {m}: HTTP {r.status_code} {r.text[:100]}", file=sys.stderr)
                    if r.status_code in (429, 503):
                        bot.BUSY.add(m)
                    continue
                cand = (r.json().get("candidates") or [{}])[0]
                txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
                out = json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip())
                if isinstance(out, dict) and out:
                    print(f"[info] [{tag}] {m} 성공")
                    return out
            except Exception as ex:
                print(f"[warn] [{tag}] {m}: {ex}", file=sys.stderr)
        time.sleep(8)
    return None


# ───────────────────────── 시세(Yahoo) ─────────────────────────
US_IDX = [("^GSPC", "S&P500"), ("^IXIC", "나스닥"), ("^DJI", "다우"), ("^RUT", "러셀2000"), ("^SOX", "필라델피아 반도체"), ("^VIX", "VIX")]
MACRO = [("^TNX", "미 국채 10년(%)"), ("DX-Y.NYB", "달러인덱스"), ("JPY=X", "달러/엔"),
         ("CL=F", "WTI($)"), ("GC=F", "금($)"), ("BTC-USD", "비트코인($)")]
SECTORS = [("XLK", "기술"), ("XLC", "커뮤니케이션"), ("XLY", "경기소비재"), ("XLP", "필수소비재"), ("XLE", "에너지"),
           ("XLF", "금융"), ("XLV", "헬스케어"), ("XLI", "산업재"), ("XLB", "소재"), ("XLU", "유틸리티"), ("XLRE", "부동산")]
MEGA = ("AAPL MSFT NVDA GOOGL AMZN META AVGO TSLA BRK-B LLY JPM V WMT ORCL MA XOM UNH COST NFLX HD PG JNJ ABBV BAC "
        "CRM AMD KO CVX MRK PEP TMO ADBE CSCO MCD WFC LIN ACN ABT PM GE IBM INTU QCOM TXN NOW ISRG CAT DIS AMAT GS "
        "UBER MU LRCX PLTR ANET PANW KLAC BKNG SPGI AXP MS RTX HON BA NKE SBUX INTC ARM SHOP TSM ASML NVO").split()
JP_IDX = [("^N225", "닛케이225"), ("1306.T", "TOPIX(ETF 1306)"), ("2516.T", "그로스250(ETF 2516)"), ("JPY=X", "달러/엔")]


def quotes(syms):
    """{심볼: {px, chg(%), d(전일 대비), date}} — 직전 거래일 종가 기준"""
    import yfinance as yf
    out = {}
    try:
        df = yf.download(syms, period="10d", interval="1d", auto_adjust=False, group_by="ticker", threads=True, progress=False)
    except Exception as ex:
        print(f"[warn] Yahoo 시세 실패: {ex}", file=sys.stderr)
        return out
    for s in syms:
        try:
            c = (df[s]["Close"] if len(syms) > 1 else df["Close"]).dropna()
            if len(c) < 2:
                continue
            a, b = float(c.iloc[-2]), float(c.iloc[-1])
            out[s] = {"px": b, "d": b - a, "chg": (b / a - 1) * 100 if a else None, "date": c.index[-1].strftime("%Y-%m-%d")}
        except Exception:
            pass
    return out


def finviz_rows(f, order, n=10):
    """finviz 스크리너 표 → [{tk, name, sector, ind, cap, pct}] (티커는 페이지 주석의 실제 값으로 맞춤)"""
    import highs
    rows = []
    try:
        h = highs.get("https://finviz.com/screener.ashx", params={"v": "111", "f": f, "o": order})
        ts = re.search(r"<!-- TS\s*(.*?)\s*TE -->", h, re.S)
        real = {}
        for x in (ts.group(1).split() if ts else []):
            p = x.split("|")
            if len(p) == 3:
                real[(highs.num(p[1]), highs.num(p[2]))] = p[0]
        for tr in re.findall(r'<tr class="styled-row[^"]*"[^>]*>(.*?)</tr>', h, re.S):
            c = [highs.text_of(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(c) < 11:
                continue
            tk = real.get((highs.num(c[8]), highs.num(c[10])))
            if tk:
                rows.append({"tk": tk, "name": c[2], "sector": c[3], "ind": c[4], "cap": c[6], "pct": highs.num(c[9])})
    except Exception as ex:
        print(f"[warn] finviz 실패({f}, {order}): {ex}", file=sys.stderr)
    return rows[:n]


def us_earnings():
    """오늘 밤(미국 현지 '내일') 실적 발표 대형주(시총 100억 달러↑): (장 전, 장 후)"""
    return (finviz_rows("cap_largeover,earningsdate_tomorrowbefore", "-marketcap", 15),
            finviz_rows("cap_largeover,earningsdate_tomorrowafter", "-marketcap", 15))


def finviz_movers():
    """S&P500 당일 상승·하락 상위(finviz). 실패하면 빈 목록"""
    return finviz_rows("idx_sp500", "-change"), finviz_rows("idx_sp500", "change")


# ───────────────────────── 텔레그램 공개 채널 글 ─────────────────────────
KR_POST = re.compile(r"한국 증시|코스피|코스닥|서학개미|KOSPI|야간선물|원/달러|원·달러")  # 한국 관련 글은 빼고 읽음
def tg_posts(ch, since, limit_chars=22000):
    """t.me/s/채널(공개 미리보기)에서 since 이후 글 본문 목록 [(시각, 글)]"""
    out, url = [], f"https://t.me/s/{ch}"
    for _ in range(4):
        try:
            h = requests.get(url, headers=UA, timeout=20).text
        except Exception as ex:
            print(f"[warn] 텔레그램 채널 {ch} 실패: {ex}", file=sys.stderr)
            break
        blocks = h.split('class="tgme_widget_message_wrap')[1:]
        if not blocks:
            break
        ids, older = [], False
        for b in blocks:
            pid = re.search(r'data-post="[^/"]+/(\d+)"', b)
            t = re.search(r'<time[^>]*datetime="([^"]+)"', b)
            m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
            if pid:
                ids.append(int(pid.group(1)))
            if not (t and m):
                continue
            ts = datetime.fromisoformat(t.group(1)).astimezone(JST)
            if ts < since:
                older = True
                continue
            txt = H.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", m.group(1)))).strip()
            if len(txt) >= 40 and not KR_POST.search(txt[:120]):
                out.append((ts, txt, int(pid.group(1)) if pid else 0))
        if older or not ids:
            break
        url = f"https://t.me/s/{ch}?before={min(ids)}"
        time.sleep(1)
    out = sorted(set(out))
    key = re.compile(r"브리핑|마감|미 증시|미국 증시|특징 종목|증시 관련 수치|실적 요약|섹터")
    pri = sorted(out, key=lambda x: (0 if key.search(x[1][:120]) else 1, -x[0].timestamp()))  # 시황·마감·브리핑 글 먼저, 그다음 최신 글
    text, total = [], 0
    for ts, t, pid in pri:  # 분량 한도까지(긴 글 하나 때문에 끊기지 않게, 넘치는 글만 건너뜀)
        if total + len(t) > limit_chars:
            continue
        text.append((ts, t, pid)); total += len(t)
    return [(ts, t) for ts, t, _ in sorted(text)]


# ───────────────────────── 다른 봇이 쌓아 둔 자료 ─────────────────────────
def load_json(path):
    try:
        return json.load(open(os.path.join(DIR, path), encoding="utf-8"))
    except Exception as ex:
        print(f"[warn] {path} 읽기 실패: {ex}", file=sys.stderr)
        return {}


def stashes(path, since_ts):
    return [x for x in load_json(path).get("pdfq", []) if x["ts"] > since_ts]


def news_items(since_ts):
    rec = load_json("state.json").get("recent", [])
    return [r for r in rec if r.get("sent") and r.get("ts", 0) > since_ts][-40:]


_JPALL = {}
def jpall_state():
    """공시봇 상태(jpall 저장소 state 브랜치): 하이라이트·실적 발표 예정일·관심 종목"""
    if not _JPALL:
        try:
            _JPALL.update(requests.get(JPALL_STATE, timeout=40).json())
        except Exception as ex:
            print(f"[warn] 공시봇 상태 읽기 실패: {ex}", file=sys.stderr)
    return _JPALL


_NAMES = {}
def jp_names():
    """종목코드 → {jp, ko, mkt} (공시봇이 JPX 목록으로 만든 한국어 이름)"""
    if not _NAMES:
        try:
            _NAMES.update(requests.get(JPALL_STATE.replace("state.json", "names.json"), timeout=40).json())
        except Exception as ex:
            print(f"[warn] 종목명 읽기 실패: {ex}", file=sys.stderr)
    return _NAMES


def jp_quotes(codes):
    """Yahoo 일괄 시세(일본 종목코드) → {코드: quote}"""
    out = {}
    try:
        from yfinance.data import YfData
        yd = YfData()
        codes = list(dict.fromkeys(codes))
        for i in range(0, len(codes), 150):
            for wait in (0, 3, 8):  # 일시적인 429 대비 재시도
                time.sleep(wait)
                try:
                    r = yd.get("https://query1.finance.yahoo.com/v7/finance/quote", params={"symbols": ",".join(c + ".T" for c in codes[i:i + 150])})
                    if r.status_code == 200:
                        for x in r.json()["quoteResponse"]["result"]:
                            out[x["symbol"][:-2]] = x
                        break
                except Exception:
                    pass
    except Exception as ex:
        print(f"[warn] 일본 종목 시세 실패: {ex}", file=sys.stderr)
    return out


def oku(yen):
    v = (yen or 0) / 1e8
    return f"{v / 10000:.1f}조엔" if v >= 10000 else f"{v:,.0f}억엔"


def jp_earnings_today(day):
    """오늘(day) 결산 발표 예정 종목 → 시총 상위 [{code, ko, sector, cap, watch}] (JPX 결산 발표 예정일, 공시봇 수집분)"""
    st = jpall_state()
    codes = [c for c, v in (st.get("sched") or {}).items() if v and v[0] == day]
    if not codes:
        return [], 0
    q = jp_quotes(codes)
    names, watch = jp_names(), st.get("watch") or {}
    try:
        import highs
        sk = highs.SECTOR_KO
    except Exception:
        sk = {}
    rows = [{"code": c, "ko": (names.get(c) or {}).get("ko") or (q.get(c) or {}).get("shortName", c),
             "sector": sk.get(st["sched"][c][1], st["sched"][c][1]) if len(st["sched"][c]) > 1 else "",
             "cap": (q.get(c) or {}).get("marketCap") or 0, "watch": c in watch} for c in codes]
    rows.sort(key=lambda r: (not r["watch"], -r["cap"]))
    return [r for r in rows if r["watch"] or r["cap"] >= 1e11][:25], len(codes)


def watch_rows(since_ts):
    """관심 종목: 전일 등락·52주 고가 근접·기간 중 공시"""
    st = jpall_state()
    codes = list((st.get("watch") or {}).keys())
    if not codes:
        return []
    q, names = jp_quotes(codes), jp_names()
    disc = {}
    for x in st.get("hl", []):
        if x.get("code") in codes and x.get("ts", 0) > since_ts and x.get("sec") != "watch":
            disc.setdefault(x["code"], []).append(x["txt"])
    for x in st.get("hl", []):  # 하이라이트 밖 관심 종목 공시(제목만)
        if x.get("code") in codes and x.get("ts", 0) > since_ts and x.get("sec") == "watch" and x["code"] not in disc:
            disc.setdefault(x["code"], []).append(x["txt"])
    rows = []
    for c in codes:
        x = q.get(c) or {}
        hi52, dh = x.get("fiftyTwoWeekHigh"), x.get("regularMarketDayHigh")
        rows.append({"code": c, "ko": (names.get(c) or {}).get("ko") or x.get("shortName", c), "px": x.get("regularMarketPrice"),
                     "chg": x.get("regularMarketChangePercent"), "cap": x.get("marketCap"),
                     "nh": bool(hi52 and dh and dh >= hi52), "gap": (x.get("regularMarketPrice") / hi52 - 1) * 100 if hi52 and x.get("regularMarketPrice") else None,
                     "disc": disc.get(c, [])})
    rows.sort(key=lambda r: -(abs(r["chg"] or 0)))
    return rows


def jgb10():
    """일본 국채 10년 금리(재무성, 전일 기준) → (기준일, 금리, 전일 대비 bp)"""
    try:
        t = requests.get("https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv", timeout=30).content.decode("cp932", "ignore")
        rows = [l.split(",") for l in t.splitlines() if re.match(r"^[RH]\d+\.\d+\.\d+,", l)]
        a, b = rows[-2], rows[-1]
        return b[0], float(b[10]), (float(b[10]) - float(a[10])) * 100
    except Exception as ex:
        print(f"[warn] 국채 금리 실패: {ex}", file=sys.stderr)
        return None


def jp_sector_rows():
    try:
        import highs
        return highs.jp_sectors()
    except Exception as ex:
        print(f"[warn] 업종 등락 실패: {ex}", file=sys.stderr)
        return []


def disclosure_items(since_ts):
    st = jpall_state()
    if not st:
        return []
    order = {"watch": 0, "beat": 1, "up": 2, "newfc": 3, "evt": 4, "ret": 5, "miss": 6, "down": 7, "prog": 8, "hold": 9, "mon": 10}
    hl = [x for x in st.get("hl", []) if x.get("ts", 0) > since_ts and x.get("sec") in order]
    hl.sort(key=lambda x: (0 if x.get("top") else 1, order[x["sec"]], -(x.get("mc") or 0)))
    seen, out = set(), []
    for x in hl:
        if (x["id"], x["sec"]) in seen:
            continue
        seen.add((x["id"], x["sec"]))
        out.append(x)
    return out[:40]


# ───────────────────────── AI 정리 ─────────────────────────
STR_ARR = {"type": "ARRAY", "items": {"type": "STRING"}}
US_SCHEMA = {"type": "OBJECT", "properties": {
    "headline": {"type": "STRING"}, "summary": STR_ARR, "sectors": STR_ARR, "macro": STR_ARR, "watch": STR_ARR,
    "stocks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "ticker": {"type": "STRING"}, "name": {"type": "STRING"}, "move": {"type": "STRING"}, "why": {"type": "STRING"}, "jp": {"type": "STRING"}},
        "required": ["ticker", "name", "why"]}}},
    "required": ["headline", "summary", "stocks"]}
US_PROMPT = """너는 한국 자산운용사 주식운용본부의 글로벌 시황 담당이다. 아래 자료로 {day} 미국 증시(현지 기준) 아침 시황을 한국어로 정리하라.
독자는 일본 주식에만 투자하는 펀드매니저다(소비재·산업재 중심). 보고서체('~함', '~했음')로 짧고 밀도 있게.

규칙
- 수치는 [시세] 표의 값만 쓴다(지수·종목 등락률을 바꾸거나 지어내지 마라). 표에 없는 수치는 참고 글에 적힌 것만, 출처 표시 없이 써도 된다.
- 한국 증시·한국 종목·코스피·원화·서학개미 등 한국 관련 내용은 쓰지 마라(참고 글에 있어도 무시).
- 참고 글(텔레그램 채널)은 내용 파악용이다. 문장을 그대로 베끼지 말고 네 말로 다시 써라.
- 사실과 해석을 구분하고, 근거 없는 해석은 '(추정)'을 붙여라.
- headline: 오늘 시장을 한 줄(40자 이내).
- summary: 시장 전체 흐름 4~6줄(지수·금리·달러·유가·주요 이벤트와 원인).
- sectors: 섹터 흐름 2~4줄(강한/약한 섹터와 이유).
- stocks: 주목할 종목 6~10개. ticker, name(한국어 통용명), move(예 "+6.6%"), why(무슨 일이 있었는지 1문장, 60자 이내).
  실적 발표·가이던스·M&A·규제 등 이벤트가 있는 종목 우선. 일본 기업(공급망·경쟁사·고객)과 연결되면 jp에 "→ 도쿄일렉트론·어드밴테스트(장비 수요)"처럼 짧게 적어라.
- macro: 금리·환율·원자재·정책·지정학 2~4줄.
- watch: 오늘 밤·이번 주 체크 포인트(실적 발표·지표·이벤트) 2~4줄.

[시세] (직전 거래일 종가 기준)
{quotes}

[S&P500 상승 상위] {gain}
[S&P500 하락 상위] {lose}

[참고 글]
{posts}
"""
JP_SCHEMA = {"type": "OBJECT", "properties": {
    "headline": {"type": "STRING"}, "summary": STR_ARR, "watch": STR_ARR, "top3": STR_ARR,
    "news": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "title": {"type": "STRING"}, "why": {"type": "STRING"}}, "required": ["title", "why"]}},
    "disc": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "i": {"type": "INTEGER"}, "why": {"type": "STRING"}}, "required": ["i", "why"]}}},
    "required": ["headline", "summary", "news", "disc"]}
JP_PROMPT = """너는 한국 자산운용사 주식운용본부의 일본 주식 담당이다. 아래 자료로 일본 증시({day} 거래일) 아침 시황을 한국어로 정리하라.
보고서체('~함')로 짧고 밀도 있게. 일본어 문자는 쓰지 말고 고유명사는 한글로.

규칙
- 수치는 [지수]·[마감 정리]·[공시]에 있는 값만 쓴다. 지어내지 마라. 근거 없는 해석은 '(추정)'.
- headline: 한 줄(40자 이내).
- summary: 지수·업종·수급·테마 흐름 4~6줄. 오늘 아침 미국 시장 흐름이 일본에 줄 영향도 1줄(추정 표시).
- news: [뉴스] 중 투자에 중요한 기사 최대 6개. title(한국어 제목 그대로 또는 짧게), why(왜 중요한지 1문장, 60자 이내).
- disc: [공시] 중 주목할 공시 최대 10개. i(공시 번호), why(투자 관점 한 줄, 60자 이내 — 수정 폭·지속성·시총 고려).
- watch: 오늘 일본 증시 체크 포인트 2~4줄(아래 [오늘 실적 발표 예정]·지표·이벤트, 자료에 있는 것만).
- top3: 오늘 아침 꼭 알아야 할 3줄(각 50자 이내). 1줄 미국, 1줄 일본, 1줄 오늘 주목할 것(공시·실적 발표·화제 중). 한국 관련 내용 금지.

[미국 증시 요약]
{us}

[지수] {idx}

[마감 정리(전 거래일 장 마감 후 작성)]
{wrap}

[뉴스(뉴스봇이 보낸 기사)]
{news}

[공시(공시봇 하이라이트)]
{disc}

[오늘 실적 발표 예정(시총 상위)]
{earn}

[커뮤니티 화제(24시간 동향)]
{com}
"""


def strip_html(s):
    return H.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def fmt_q(q, sym, name, dec=2):
    x = q.get(sym)
    if not x:
        return None
    if sym == "^TNX":
        return f"{name} {x['px']:.2f}% ({x['d'] * 100:+.0f}bp)"
    return f"{name} {x['px']:,.{dec}f} ({x['chg']:+.2f}%)"


# ───────────────────────── PDF(HTML → weasyprint) ─────────────────────────
def font_css():
    sp = importlib.util.find_spec("koreanize_matplotlib")
    d = (sp.submodule_search_locations or [""])[0] if sp else ""
    reg, bold = os.path.join(d, "fonts", "NanumGothic.ttf"), os.path.join(d, "fonts", "NanumGothicBold.ttf")
    return (f"@font-face{{font-family:'NG';src:url('file://{reg}');font-weight:400}}"
            f"@font-face{{font-family:'NG';src:url('file://{bold}');font-weight:700}}")


def chart_png(rows, title):
    """[(라벨, 등락률)] 가로 막대 → data URI"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    sp = importlib.util.find_spec("koreanize_matplotlib")
    for f in glob.glob(os.path.join((sp.submodule_search_locations or [""])[0], "fonts", "*.ttf")) if sp else []:
        fm.fontManager.addfont(f)
    plt.rcParams["font.family"] = "NanumGothic"
    plt.rcParams["axes.unicode_minus"] = False
    rows = sorted(rows, key=lambda r: r[1])
    fig, ax = plt.subplots(figsize=(6.2, 0.26 * len(rows) + 0.6), dpi=160)
    cols = ["#cf2f3f" if v >= 0 else "#2360c4" for _, v in rows]
    ax.barh([r[0] for r in rows], [r[1] for r in rows], color=cols, height=0.62)
    for i, (_, v) in enumerate(rows):
        ax.text(v + (0.03 if v >= 0 else -0.03) * max(1, max(abs(x) for _, x in rows)), i, f"{v:+.2f}%",
                va="center", ha="left" if v >= 0 else "right", fontsize=7.5, color="#333")
    ax.axvline(0, color="#999", lw=0.6)
    ax.set_title(title, loc="left", fontsize=9, color="#222")
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(labelsize=7.5, length=0)
    ax.xaxis.set_visible(False)
    lim = max(abs(v) for _, v in rows) * 1.35 or 1
    ax.set_xlim(-lim, lim)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def tg_html(s):
    """텔레그램 HTML(봇 메시지) → PDF용 HTML"""
    s = (s or "").replace("━━━━━━━━━━", "")
    s = re.sub(r"&gt;\s*(<a )", r"\1", s)  # 링크 앞 '>' 표시 제거
    s = s.replace("\n", "<br>")
    s = re.sub(r"(<br>\s*){3,}", "<br><br>", s)
    return s


def stash_html(items, title_fallback):
    out = []
    for x in items:
        head = tg_html(x.get("head", "")).replace("&gt;&gt;", "")
        bl = x.get("blocks", [])
        if x.get("kind") in ("x", "note", "weekly"):  # 줄 단위로 쌓인 메시지: 항목(빈 줄로 시작) 단위로 다시 묶음
            groups, cur = [], []
            for b in bl:
                if b.startswith("\n") and cur:
                    groups.append("\n".join(cur)); cur = []
                cur.append(b)
            if cur: groups.append("\n".join(cur))
            bl = groups
        body = "".join(f"<div class='blk'>{re.sub(r'^(<br>)+', '', tg_html(b.strip()))}</div>" for b in bl)
        out.append(f"<div class='stash'><div class='sh'>{head or esc(title_fallback)}</div>{body}</div>")
    return "".join(out)


CSS = """
@page{size:A4;margin:14mm 12mm 14mm 12mm;@bottom-right{content:counter(page) " / " counter(pages);font:8pt NG;color:#888}
      @bottom-left{content:"아침 브리핑 · %(date)s";font:8pt NG;color:#888}}
body{font-family:NG,sans-serif;font-size:9.3pt;line-height:1.55;color:#15181d}
h1{font-size:17pt;margin:0 0 2mm} .sub{color:#666;font-size:8.5pt}
h2{font-size:12.5pt;border-bottom:2px solid #9a6a12;padding-bottom:1mm;margin:6mm 0 2.5mm;page-break-after:avoid}
h2.newpage{page-break-before:always}
h3{font-size:10.5pt;margin:4mm 0 1.5mm;color:#9a6a12;page-break-after:avoid}
.hl{background:#f3e7cf;border-radius:2mm;padding:2.5mm 3.5mm;font-weight:700;font-size:10.5pt;margin:2mm 0}
ul{margin:1mm 0 2mm;padding-left:5mm} li{margin:0.6mm 0}
table{border-collapse:collapse;width:100%;font-size:8.6pt;margin:1.5mm 0 3mm}
th{background:#f1f2ee;font-weight:700;text-align:left;padding:1.2mm 2mm;border-bottom:1px solid #ccc}
td{padding:1.1mm 2mm;border-bottom:1px solid #e6e6e6;vertical-align:top}
td.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.up{color:#cf2f3f} .dn{color:#2360c4}
.grid{display:flex;gap:4mm} .grid>div{flex:1}
table.disc td:first-child{width:24%} table.disc td:nth-child(2){width:42%}
img.chart{width:100%;margin:1mm 0}
.stash{margin:2mm 0 4mm} .sh{font-weight:700;font-size:10.5pt;margin:2mm 0 1.5mm;color:#9a6a12}
.blk{border-left:2px solid #e3e3e3;padding:0.5mm 0 0.5mm 3mm;margin:0 0 2.5mm;page-break-inside:avoid}
a{color:#2360c4;text-decoration:none}
.note{color:#777;font-size:7.8pt;margin-top:1mm}
.toc{font-size:9pt;color:#444;margin:2mm 0 4mm}
.top3{border:1.5px solid #9a6a12;border-radius:2mm;padding:3mm 4mm;margin:2mm 0 4mm;font-size:10.2pt;line-height:1.7}
.t3h{font-weight:700;color:#9a6a12;margin-bottom:1mm}
.jpl{color:#9a6a12;font-size:8.2pt;margin-top:0.5mm}
table.watch td:last-child{font-size:8pt;color:#444}
"""


def cls(v):
    return "up" if (v or 0) > 0 else "dn" if (v or 0) < 0 else ""


def q_table(q, items, dec=2):
    rows = []
    for sym, name in items:
        x = q.get(sym)
        if not x:
            continue
        if sym == "^TNX":
            ch = f"{x['d'] * 100:+.0f}bp"; val = f"{x['px']:.2f}%"; c = cls(x["d"])
        else:
            ch = f"{x['chg']:+.2f}%"; val = f"{x['px']:,.{dec}f}"; c = cls(x["chg"])
        rows.append(f"<tr><td>{esc(name)}</td><td class='n'>{val}</td><td class='n {c}'>{ch}</td></tr>")
    return "<table><tr><th>항목</th><th>종가</th><th>등락</th></tr>" + "".join(rows) + "</table>" if rows else ""


def li(lines):
    return "<ul>" + "".join(f"<li>{esc(str(x).lstrip('•·- '))}</li>" for x in (lines or []) if str(x).strip()) + "</ul>"


def build_pdf(state, since_ts):
    t = now()
    since = datetime.fromtimestamp(since_ts, JST)
    # ── 1) 미국
    q = quotes([s for s, _ in US_IDX + MACRO + SECTORS + JP_IDX] + MEGA)
    us_day = (q.get("^GSPC") or {}).get("date", "")
    gain, lose = finviz_movers()
    ern_pre, ern_post = us_earnings()
    posts = []
    for ch, label in CHANNELS:
        for ts, txt in tg_posts(ch, t - timedelta(hours=18)):
            posts.append(f"### {label} {ts:%m/%d %H:%M}\n{txt}")
    qtxt = "\n".join(filter(None, [fmt_q(q, s, n) for s, n in US_IDX + MACRO + SECTORS]))
    mega = sorted([(s, q[s]["chg"]) for s in MEGA if s in q and q[s]["chg"] is not None], key=lambda x: -x[1])
    qtxt += "\n[대형주] " + ", ".join(f"{s} {c:+.2f}%" for s, c in mega)
    ftxt = lambda rows: ", ".join(f"{r['tk']}({r['name']}, {r['ind']}) {r['pct']:+.2f}%" for r in rows if r.get("pct") is not None) or "(없음)"
    us = gemini_obj(state, US_PROMPT.format(day=us_day, quotes=qtxt, gain=ftxt(gain), lose=ftxt(lose),
                                            posts="\n\n".join(posts)[:42000] or "(없음)"), US_SCHEMA, "미국 시황") or {}
    # ── 2) 일본
    wraps = stashes("highs_state.json", since_ts)
    wrap = [x for x in wraps if x["kind"] == "wrap"]
    news = news_items(since_ts)
    disc = disclosure_items(since_ts)
    jp_idx = ", ".join(filter(None, [fmt_q(q, s, n) for s, n in JP_IDX]))
    jp_day = (q.get("^N225") or {}).get("date", "")
    wrap_txt = "\n".join(strip_html(x["head"]) + "\n" + "\n".join(strip_html(b) for b in x["blocks"]) for x in wrap[-2:]) or "(없음)"
    news_txt = "\n".join(f"#{i + 1} {r.get('ko', '')} — {r.get('gist', '')}" for i, r in enumerate(news)) or "(없음)"
    disc_txt = "\n".join(f"#{i + 1} {x['name']}({x['code']}, 시총 {x.get('mct') or '?'}) [{x['sec']}] {x['txt']}"
                         + (f" / 사유: {x.get('why') or x.get('why0')}" if (x.get('why') or x.get('why0')) else "")
                         for i, x in enumerate(disc)) or "(없음)"
    jgb = jgb10()
    if jgb:
        jp_idx += f", 일본 국채 10년 {jgb[1]:.3f}% ({jgb[2]:+.1f}bp, {jgb[0]})"
    secs = jp_sector_rows()
    if secs:
        jp_idx += "\n[33업종 등락 상위] " + ", ".join(f"{k} {v:+.2f}%" for k, v in secs[:5]) + " / [하위] " + ", ".join(f"{k} {v:+.2f}%" for k, v in secs[::-1][:5])
    earn, earn_n = jp_earnings_today(t.strftime("%Y-%m-%d"))
    earn_txt = ", ".join(f"{r['ko']}({r['code']}, {oku(r['cap'])}{', 관심 종목' if r['watch'] else ''})" for r in earn) or "(없음)"
    watch = watch_rows(since_ts)
    # ── 3)·4) 쌓아 둔 메시지
    com = stashes("community_state.json", since_ts)
    com_txt = "\n".join(strip_html(b)[:1500] for x in com if x["kind"] == "community" for b in x["blocks"][:1])[:3000] or "(없음)"
    us_sum = " ".join(us.get("summary") or [])[:800] or qtxt[:600]
    jp = gemini_obj(state, JP_PROMPT.format(day=jp_day, us=us_sum, idx=jp_idx or "-", wrap=wrap_txt[:6000], news=news_txt[:8000],
                                            disc=disc_txt[:9000], earn=earn_txt, com=com_txt), JP_SCHEMA, "일본 시황") or {}
    hi = [x for x in wraps if x["kind"] in ("highs", "gainers")]

    # ── HTML
    wd = "월화수목금토일"[t.weekday()]
    parts = [f"<h1>아침 브리핑 {t:%Y-%m-%d}({wd})</h1>",
             f"<div class='sub'>{t:%H:%M} KST 작성 · 대상 기간 {since:%m/%d %H:%M} ~ {t:%m/%d %H:%M} · 미국 {us_day or '-'} 종가 · 일본 {jp_day or '-'} 종가</div>",
             "<div class='toc'>1. 글로벌 시황 · 2. 일본 증시 시황(실적 발표 예정·관심 종목) · 3. 커뮤니티·X·note 화제 · 4. 일본 52주 신고가·상승률 상위</div>"]
    top3 = [x for x in (jp.get("top3") or []) if str(x).strip()][:3] or [x for x in (us.get("headline"), jp.get("headline")) if x]
    if top3:
        parts.append("<div class='top3'><div class='t3h'>오늘의 3줄 요약</div>" + "".join(f"<div>{i}. {esc(str(x).lstrip('•·- 0123456789.'))}</div>" for i, x in enumerate(top3, 1)) + "</div>")
    # 1
    parts.append("<h2>1. 글로벌 시황 (미국 중심)</h2>")
    if us.get("headline"):
        parts.append(f"<div class='hl'>{esc(us['headline'])}</div>")
    parts.append("<div class='grid'><div><h3>지수</h3>" + q_table(q, US_IDX) + "</div><div><h3>금리·환율·원자재</h3>"
                 + q_table(q, MACRO) + "</div></div>")
    if us.get("summary"):
        parts.append("<h3>시장 흐름</h3>" + li(us["summary"]))
    sec_rows = [(n, q[s]["chg"]) for s, n in SECTORS if s in q and q[s]["chg"] is not None]
    if sec_rows:
        try:
            parts.append(f"<img class='chart' src='{chart_png(sec_rows, 'S&P500 섹터 ETF 등락률(%)')}'>")
        except Exception as ex:
            print(f"[warn] 섹터 차트 실패: {ex}", file=sys.stderr)
    if us.get("sectors"):
        parts.append("<h3>섹터</h3>" + li(us["sectors"]))
    if us.get("stocks"):
        parts.append("<h3>주목 종목</h3><table><tr><th>종목</th><th>등락</th><th>내용</th></tr>" + "".join(
            f"<tr><td>{esc(s.get('name', ''))} <span class='note'>{esc(s.get('ticker', ''))}</span></td>"
            f"<td class='n {cls(-1 if str(s.get('move', '')).startswith('-') else 1)}'>{esc(s.get('move', ''))}</td><td>{esc(s.get('why', ''))}"
            + (f"<div class='jpl'>🇯🇵 {esc(s['jp'].lstrip('→ '))}</div>" if (s.get('jp') or '').strip() else "") + "</td></tr>"
            for s in us["stocks"]) + "</table>")
    if gain or lose:
        mk = lambda rows: "".join(f"<tr><td>{esc(r['tk'])} <span class='note'>{esc(r['name'][:28])}</span></td><td class='n {cls(r['pct'])}'>{r['pct']:+.2f}%</td></tr>"
                                  for r in rows[:8] if r.get("pct") is not None)
        parts.append("<div class='grid'><div><h3>S&P500 상승 상위</h3><table>" + mk(gain) + "</table></div><div><h3>S&P500 하락 상위</h3><table>"
                     + mk(lose) + "</table></div></div>")
    if ern_pre or ern_post:
        ek = lambda rows: "".join(f"<tr><td>{esc(r['tk'])} <span class='note'>{esc(r['name'][:26])}</span></td><td class='n'>{esc(r['cap'])}</td></tr>" for r in rows) or "<tr><td class='note'>없음</td><td></td></tr>"
        parts.append("<h3>오늘 밤 실적 발표(미국, 시총 100억 달러↑)</h3><div class='grid'><div><table><tr><th>장 전</th><th>시총($B)</th></tr>" + ek(ern_pre)
                     + "</table></div><div><table><tr><th>장 마감 후</th><th>시총($B)</th></tr>" + ek(ern_post) + "</table></div></div>")
    if us.get("macro"):
        parts.append("<h3>매크로</h3>" + li(us["macro"]))
    if us.get("watch"):
        parts.append("<h3>체크 포인트</h3>" + li(us["watch"]))
    if not us:
        parts.append("<p class='note'>(AI 정리 실패: 시세 표만 싣습니다)</p>")
    parts.append(f"<div class='note'>시세: Yahoo Finance(직전 거래일 종가) · 상승·하락 상위: finviz · 참고: 텔레그램 채널 "
                 f"{', '.join(l for _, l in CHANNELS)}의 공개 글({len(posts)}건)을 AI가 재정리.</div>")
    # 2
    parts.append("<h2 class='newpage'>2. 일본 증시 시황</h2>")
    if jp.get("headline"):
        parts.append(f"<div class='hl'>{esc(jp['headline'])}</div>")
    jt = q_table(q, JP_IDX)
    if jgb and jt:
        jt = jt.replace("</table>", f"<tr><td>일본 국채 10년 <span class='note'>{esc(jgb[0])}</span></td><td class='n'>{jgb[1]:.3f}%</td><td class='n {cls(jgb[2])}'>{jgb[2]:+.1f}bp</td></tr></table>")
    parts.append(jt)
    if jp.get("summary"):
        parts.append("<h3>시장 흐름</h3>" + li(jp["summary"]))
    if secs:
        try:
            pick = secs[:8] + [x for x in secs[-8:] if x not in secs[:8]]
            parts.append(f"<img class='chart' src='{chart_png(pick, '33업종 등락률 상·하위 8(%) · 전 거래일')}'>")
        except Exception as ex:
            print(f"[warn] 업종 차트 실패: {ex}", file=sys.stderr)
    if wrap:
        w = wrap[-1]
        parts.append("<h3>전 거래일 마감 정리</h3>" + "".join(f"<div class='blk'>{tg_html(b)}</div>" for b in w["blocks"][:-1]))
    if not jp.get("news") and news:  # AI 실패: 뉴스봇이 보낸 기사 상위 8개 그대로
        jp["news"] = [{"title": r.get("ko", ""), "why": r.get("gist", "")[:80]} for r in news[-8:]]
    if jp.get("news"):
        parts.append("<h3>주목 뉴스</h3><table><tr><th>기사</th><th>왜 중요한가</th></tr>" + "".join(
            f"<tr><td>{esc(n.get('title', ''))}</td><td>{esc(n.get('why', ''))}</td></tr>" for n in jp["news"]) + "</table>")
    if disc:
        picks = [(d["i"] - 1, d.get("why", "")) for d in (jp.get("disc") or []) if isinstance(d.get("i"), int) and 0 < d["i"] <= len(disc)]
        if not picks:
            picks = [(i, disc[i].get("why") or disc[i].get("why0") or "") for i in range(min(10, len(disc)))]
        parts.append("<h3>주목 공시</h3><table class='disc'><tr><th>기업</th><th>내용</th><th>포인트</th></tr>" + "".join(
            f"<tr><td>{esc(disc[i]['name'])} <span class='note'>{esc(disc[i]['code'])} · {esc(disc[i].get('mct') or '')}</span></td>"
            f"<td>{esc(disc[i]['txt'])}</td><td>{esc(why)}</td></tr>" for i, why in picks) + "</table>")
    if earn:
        parts.append(f"<h3>오늘 실적 발표 예정 <span class='note'>전체 {earn_n}종목 중 시총 1,000억엔↑·관심 종목, 시총 순</span></h3>"
                     "<table><tr><th>종목</th><th>업종</th><th>시총</th></tr>" + "".join(
                         f"<tr><td>{'⭐ ' if r['watch'] else ''}{esc(r['ko'])} <span class='note'>{esc(r['code'])}</span></td><td>{esc(r['sector'])}</td>"
                         f"<td class='n'>{oku(r['cap']) if r['cap'] else '-'}</td></tr>" for r in earn) + "</table>")
    if watch:
        def wrow(r):
            px = f"{r['px']:,.0f}" if r["px"] else "-"
            ch = f"{r['chg']:+.2f}%" if r["chg"] is not None else "-"
            gap = f"{r['gap']:+.1f}%" if r["gap"] is not None else "-"
            nh = " <b class='up'>신고가</b>" if r["nh"] else ""
            return (f"<tr><td>{esc(r['ko'])} <span class='note'>{esc(r['code'])}</span>{nh}</td><td class='n'>{px}</td>"
                    f"<td class='n {cls(r['chg'])}'>{ch}</td><td class='n'>{gap}</td><td>{esc(' / '.join(r['disc'])[:160])}</td></tr>")
        parts.append("<h3>관심 종목 <span class='note'>전 거래일 등락 큰 순 · 기간 중 공시</span></h3><table class='watch'><tr><th>종목</th><th>종가</th>"
                     "<th>등락</th><th>52주 고가 대비</th><th>공시</th></tr>" + "".join(wrow(r) for r in watch) + "</table>")
    if jp.get("watch"):
        parts.append("<h3>체크 포인트</h3>" + li(jp["watch"]))
    parts.append("<div class='note'>지수: Yahoo Finance(TOPIX·그로스250은 연동 ETF로 대용) · 마감 정리: 카부탄·피스코 마감 시황 기반 신고가 봇 요약 · "
                 f"뉴스 {len(news)}건(뉴스봇 발송분) · 공시 {len(disc)}건(공시봇 하이라이트) · '(추정)'은 근거 없는 해석.</div>")
    # 3
    order = ["urgent", "community", "x", "note", "weekly", "insight"]
    com.sort(key=lambda x: (order.index(x["kind"]) if x["kind"] in order else 9, x["ts"]))
    parts.append("<h2 class='newpage'>3. 커뮤니티·X·note 화제</h2>")
    parts.append(stash_html(com, "화제") if com else "<p class='note'>이 기간에 쌓인 화제글 정리가 없습니다.</p>")
    # 4
    parts.append("<h2 class='newpage'>4. 일본 52주 신고가 · 상승률 상위</h2>")
    parts.append(stash_html(sorted(hi, key=lambda x: (x.get("day", ""), x["kind"] != "highs")), "신고가")
                 if hi else "<p class='note'>이 기간에 쌓인 신고가·상승률 정리가 없습니다(휴장일이거나 아직 발송 전).</p>")
    doc = (f"<html><head><meta charset='utf-8'><style>{font_css()}{CSS.replace('%(date)s', t.strftime('%Y-%m-%d'))}</style></head><body>"
           + "".join(parts) + "</body></html>")
    from weasyprint import HTML
    pdf = HTML(string=doc, base_url=DIR).write_pdf()
    cap = [f"<b>아침 브리핑 {t:%m/%d}({wd})</b>"]
    if top3:
        cap += [f"{i}. {esc(str(x).lstrip('•·- 0123456789.'))}" for i, x in enumerate(top3, 1)]
    cap.append(f"화제글·트렌드 {len(com)}건 · 신고가·상승률 {len(hi)}건")
    return pdf, "\n".join(cap)


def send_pdf(pdf, caption, name):
    if DRY_RUN:
        open(os.path.join(DIR, "morning.pdf"), "wb").write(pdf)
        print("──── PDF 저장(DRY_RUN) ────\n" + caption)
        return True
    tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendDocument",
                          data={"chat_id": chat, "caption": caption[:1000], "parse_mode": "HTML"},
                          files={"document": (name, pdf, "application/pdf")}, timeout=120)
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        print(f"[warn] telegram {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    return False


def main():
    wait = "--wait" in sys.argv
    if "--catchup" in sys.argv:  # 이어 달리기 시작 때: 08:00~11:00 사이이고 아직 안 보냈으면 지금 만들어 08:30에(지났으면 바로) 보냄
        if (now().hour, now().minute) < (8, 0) and not FORCE:
            print("[info] 08:00 전: 따라잡기 대상 아님"); return
        wait = True
    st = load_state()
    t = now()
    today = t.strftime("%Y-%m-%d")
    if not FORCE:
        if t.weekday() >= 5:
            print("[info] 주말: 보내지 않음(월요일 PDF에 합산)"); return
        if st.get("sent") == today:
            print(f"[info] 오늘({today}) 이미 보냄"); return
        if t.hour >= LATE_H:
            print(f"[info] 너무 늦음({t:%H:%M}) → 오늘은 건너뜀"); return
        if not wait and (t.hour, t.minute) < SEND_AT:
            print(f"[info] 아직 {SEND_AT[0]:02d}:{SEND_AT[1]:02d} 전"); return
    since_ts = st.get("last_ts") or (time.time() - 26 * 3600)
    since_ts = max(since_ts, time.time() - 4 * 86400)  # 오래 끊겼어도 최대 4일치
    t_start = time.time()
    pdf, cap = build_pdf(st, since_ts)
    print(f"[info] PDF {len(pdf) / 1024:.0f}KB, {time.time() - t_start:.0f}초")
    if wait and not FORCE:
        target = now().replace(hour=SEND_AT[0], minute=SEND_AT[1], second=0, microsecond=0)
        sec = (target - now()).total_seconds()
        if sec > 0:
            print(f"[info] {sec / 60:.0f}분 대기 후 발송"); time.sleep(sec)
    if send_pdf(pdf, cap, f"아침브리핑_{today}.pdf"):
        if not FORCE:  # 테스트 발송(MORNING_NOW)은 기간 기준을 바꾸지 않음
            st["last_ts"] = t_start
            st["sent"] = today
        if not DRY_RUN:
            save_state(st)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
