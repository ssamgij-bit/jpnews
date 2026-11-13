"""평일 08:30(KST) 아침 브리핑 PDF → 텔레그램.

구성
 1) 글로벌 시황(미국 중심): 지수·금리·환율·원자재·섹터 ETF·대형주 등락(Yahoo), S&P500 상승·하락 상위(finviz),
    참고 텔레그램 채널(사제콩이 @ehdwl, 미국 주식 인사이더 @insidertracking)의 아침 글 → AI 정리
 2) 일본 증시 시황: 지수(Yahoo), 신고가 봇의 마감 정리(highs_state.json), 뉴스봇이 보낸 기사(state.json),
    공시봇 하이라이트(jpall 저장소 state 브랜치) → AI 정리
 3) (커뮤니티 화제글·X·note 트렌드는 2026-11-13부터 텔레그램 채널 07:00 발송으로 옮겨 PDF에서 뺌)
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


def gemini_obj(state, prompt, schema, tag, check=None):
    """객체 JSON 응답(AI). 실패하면 None. check(out)가 거짓이면(내용이 빈약·깨짐) 다음 모델로 — 끝까지 없으면 가장 나은 것"""
    best = None
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
                    if check and not check(out):
                        print(f"[warn] [{tag}] {m}: 응답이 빈약하거나 깨짐 → 다음 모델", file=sys.stderr)
                        best = best or out
                        continue
                    print(f"[info] [{tag}] {m} 성공")
                    return out
            except Exception as ex:
                print(f"[warn] [{tag}] {m}: {ex}", file=sys.stderr)
        time.sleep(8)
    return best


MOVE_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?%$")


def us_ok(o):
    st = [x for x in (o.get("stocks") or []) if MOVE_RE.match(str(x.get("move", "")).strip())]
    return len(o.get("summary") or []) >= 3 and len(st) >= 4 and (o.get("sectors") or o.get("macro"))


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
            n = now()
            if (s.endswith(".T") or s == "^N225") and len(c) and c.index[-1].strftime("%Y-%m-%d") == n.strftime("%Y-%m-%d") and (n.hour, n.minute) < (15, 30):
                c = c.iloc[:-1]   # 장중(테스트 발송 등): 오늘 봉은 아직 확정 전
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


_WATCH = []
def watch_codes():
    """관심 종목 = japan stock 봇(@JP_stock_yh_bot) 주인 구독 목록(한 곳에서 관리).
    공시봇 state의 관심 종목은 암호화돼 있어 직접 못 읽음 → 워커 /watchlist 를 WATCH_KEY로 읽음"""
    if not _WATCH:
        key = os.environ.get("WATCH_KEY", "")
        try:
            if key:
                j = requests.get(os.environ.get("WATCH_URL", "https://jp-disclosure-bot.ssamgij.workers.dev/watchlist"),
                                 headers={"x-watch-key": key, "User-Agent": "Mozilla/5.0"}, timeout=30).json()
                _WATCH.extend(str(c)[:4].upper() for c in (j.get("codes") or []))
        except Exception as ex:
            print(f"[warn] 관심 종목 읽기 실패: {ex}", file=sys.stderr)
        if not _WATCH:
            _WATCH.extend((jpall_state().get("watch") or {}).keys())
        if not _WATCH:
            print("[warn] 관심 종목 없음(WATCH_KEY 비밀값 확인)", file=sys.stderr)
    return _WATCH


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
    names, watch = jp_names(), set(watch_codes())
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


def watch_calendar(t, days=7):
    """관심 종목 중 오늘부터 days일 안에 결산 발표가 잡힌 종목 → [(날짜, [(코드, 이름)])] 날짜순 (JPX 결산 발표 예정일, 공시봇 수집분)"""
    st = jpall_state()
    sched = st.get("sched") or {}
    names = jp_names()
    lo, hi = t.strftime("%Y-%m-%d"), (t + timedelta(days=days - 1)).strftime("%Y-%m-%d")
    by = {}
    for c in watch_codes():
        v = sched.get(c)
        if v and lo <= v[0] <= hi:
            by.setdefault(v[0], []).append((c, (names.get(c) or {}).get("ko") or c))
    return sorted(by.items())


def watch_rows(since_ts):
    """관심 종목: 전일 등락·52주 고가 근접·기간 중 공시"""
    st = jpall_state()
    codes = list(watch_codes())
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


EARN_STATE = "https://raw.githubusercontent.com/ssamgij-bit/jpall/earn-state/earn.json"


def earnings_reaction(jp_day):
    """전 거래일(jp_day) 주가 반응: 그 전 거래일에 실적 발표한 종목(실적 스크리닝 봇 기록) — 주로 장 마감 후 발표라 다음 거래일에 반영.
    → [{code, ko, chg, cap, pass, tag, txt}], 발표일"""
    if not jp_day:
        return [], ""
    try:
        log = requests.get(EARN_STATE, timeout=60).json().get("log") or []
    except Exception as ex:
        print(f"[warn] 실적 봇 기록 읽기 실패: {ex}", file=sys.stderr)
        return [], ""
    jd = jp_day.replace("-", "")
    days = sorted({x[0] for x in log if x and x[0] < jd})
    if not days:
        return [], ""
    d0 = days[-1]
    rows = {}
    for x in log:
        if x[0] == d0:
            rows[x[1]] = x
    if not rows:
        return [], d0
    q, names, watch = jp_quotes(list(rows)), jp_names(), set(watch_codes())
    hl = {}
    for h in jpall_state().get("hl", []):
        if h.get("code") in rows and h.get("sec") in ("beat", "miss", "up", "down", "newfc", "prog", "watch"):
            hl.setdefault(h["code"], h.get("txt", ""))
    out = []
    for c, x in rows.items():
        qq = q.get(c) or {}
        chg, cap = qq.get("regularMarketChangePercent"), qq.get("marketCap") or 0
        if chg is None or not (cap >= 3e10 or c in watch):
            continue
        out.append({"code": c, "ko": (names.get(c) or {}).get("ko") or qq.get("shortName", c), "chg": chg, "cap": cap,
                    "pass": bool(x[3]), "tag": x[4] if len(x) > 4 else "", "txt": hl.get(c, ""), "watch": c in watch})
    up = sorted([r for r in out if r["chg"] > 0], key=lambda r: -r["chg"])[:8]
    dn = sorted([r for r in out if r["chg"] <= 0], key=lambda r: r["chg"])[:8]
    return up + dn, d0


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


def jgb10_hist(years=5):
    """일본 국채 10년 금리 일별 이력(재무성: 과거분 jgbcm_all + 이번 달 jgbcm) → [(datetime, %)] 최근 years년"""
    out = {}
    for u in ("https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv",
              "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"):
        try:
            t = requests.get(u, timeout=60).content.decode("cp932", "ignore")
        except Exception as ex:
            print(f"[warn] 국채 이력 실패 {u}: {ex}", file=sys.stderr)
            continue
        for l in t.splitlines():
            m = re.match(r"^([RH])(\d+)\.(\d+)\.(\d+),", l)
            if not m:
                continue
            c = l.split(",")
            try:
                v = float(c[10])
            except (ValueError, IndexError):
                continue
            y = int(m.group(2)) + (2018 if m.group(1) == "R" else 1988)
            out[datetime(y, int(m.group(3)), int(m.group(4)))] = v
    cut = datetime.now() - timedelta(days=365 * years + 5)
    return sorted((d, v) for d, v in out.items() if d >= cut)


def usdjpy_hist(years=5):
    """달러/엔 일봉 종가(Yahoo) → [(datetime, 값)] 최근 years년"""
    try:
        import yfinance as yf
        c = yf.download("JPY=X", period=f"{years}y", interval="1d", auto_adjust=False, progress=False)["Close"].dropna()
        if hasattr(c, "columns"):
            c = c.iloc[:, 0]
        return [(i.to_pydatetime().replace(tzinfo=None), float(v)) for i, v in c.items()]
    except Exception as ex:
        print(f"[warn] 달러/엔 이력 실패: {ex}", file=sys.stderr)
        return []


def rates_chart_png(fx, jgb):
    """달러/엔·일본 국채 10년 최근 5년 선 차트(좌우 2칸) → data URI. 최근값과 1년 전 대비 변화를 제목에"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib import font_manager as fm
    sp = importlib.util.find_spec("koreanize_matplotlib")
    for f in glob.glob(os.path.join((sp.submodule_search_locations or [""])[0], "fonts", "*.ttf")) if sp else []:
        fm.fontManager.addfont(f)
    plt.rcParams["font.family"] = "NanumGothic"
    plt.rcParams["axes.unicode_minus"] = False
    panels = [(fx, "달러/엔", lambda v: f"{v:,.2f}엔", lambda a, b: f"{b - a:+.1f}엔", "#9a6a12"),
              (jgb, "일본 국채 10년(%)", lambda v: f"{v:.3f}%", lambda a, b: f"{(b - a) * 100:+.0f}bp", "#23272e")]
    panels = [p for p in panels if len(p[0]) > 20]
    if not panels:
        return None
    fig, axes = plt.subplots(1, len(panels), figsize=(7.4 if len(panels) == 2 else 4.2, 2.3), dpi=160)
    axes = axes if len(panels) > 1 else [axes]
    for ax, (ser, name, fmt, dfmt, col) in zip(axes, panels):
        xs, ys = [d for d, _ in ser], [v for _, v in ser]
        ax.plot(xs, ys, color=col, lw=1.1)
        last_d, last = xs[-1], ys[-1]
        y1 = next((v for d, v in reversed(ser) if (last_d - d).days >= 365), ys[0])
        ax.scatter([last_d], [last], color=col, s=10, zorder=3)
        hi, lo = max(ys), min(ys)
        ax.set_title(f"{name}  {fmt(last)}  (1년 전 대비 {dfmt(y1, last)})\n", loc="left", fontsize=8.5, color="#222")
        ax.text(0, 1.02, f"5년 최고 {fmt(hi)} · 최저 {fmt(lo)} · {last_d:%Y-%m-%d} 기준", transform=ax.transAxes, fontsize=6.8, color="#888")
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1,)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax.tick_params(labelsize=6.8, length=0)
        ax.grid(axis="y", color="#eee", lw=0.6)
        for s_ in ("top", "right", "left"):
            ax.spines[s_].set_visible(False)
        ax.spines["bottom"].set_color("#ccc")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


SIZE_W = {"Core30": 8, "Large70": 3, "Mid400": 1}   # 업종 지수 영향 가중(시총 규모 대용)


DIGEST_RE = re.compile(r"前日に動いた銘柄|本日の注目個別銘柄|話題株ダイジェスト|注目銘柄|動いた銘柄")


def stock_evidence(code, jp_day):
    """종목 등락 이유 근거(카부탄): 그 거래일 전후 종목 기사 본문 + 시황 다이제스트 속 해당 종목 문단 + 최근 제목. → 문자열 목록"""
    import highs
    out = []
    try:
        top = highs.get(f"https://s.kabutan.jp/stocks/{code}/")
        m = re.search(r"<title>\s*(.+?)【", top)
        short = highs.text_of(m.group(1)).strip() if m else ""
        h = highs.get(f"https://s.kabutan.jp/stocks/{code}/news/")
    except Exception as ex:
        print(f"[warn] 카부탄 {code}: {ex}", file=sys.stderr)
        return out
    d0 = datetime.strptime(jp_day, "%Y-%m-%d") if jp_day else now().replace(tzinfo=None)
    lo, hi = (d0 - timedelta(days=2)).strftime("%Y-%m-%d"), (d0 + timedelta(days=3)).strftime("%Y-%m-%d")
    own, dig, titles = [], [], []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", h, re.S):
        t = re.search(r'datetime="(\d{4}-\d\d-\d\d) (\d\d:\d\d)', li)
        a = re.search(r'href="(/stocks/[^"]+/news/[^"]+)"', li)
        tt = re.search(r'<div class="w-full[^"]*">(.*?)</div>', li, re.S)
        if not (t and tt) or not (lo <= t.group(1) <= hi):
            continue
        title = highs.text_of(tt.group(1)).strip()
        titles.append(f"{t.group(1)[5:]} {title[:90]}")
        if not a:
            continue
        if DIGEST_RE.search(title):
            dig.append((title, a.group(1)))
        elif (short and short in title) or re.search(r"決算|上方修正|下方修正|目標株価|格上げ|格下げ|受注|買収|ストップ", title):
            own.append((title, a.group(1)))
    for title, u in own[:2]:
        try:
            out.append(f"[기사] {title} — {highs._article(u, 700)}")
        except Exception:
            pass
        time.sleep(0.3)
    for title, u in dig[:3]:
        try:
            body = highs._article(u, 20000)
            i = body.find(f"<{code}>")   # 다이제스트는 '종목명 <코드>' 형식
            if i < 0 and short:
                i = body.find(short)
            if i >= 0:
                out.append(f"[시황 다이제스트 발췌] {body[max(0, i - 40):i + 260]}")
        except Exception:
            pass
        time.sleep(0.3)
    if titles:
        out.append("[최근 기사 제목] " + " / ".join(titles[:8]))
    return out


def sector_movers(secs, n_sec=3, n_stk=3, jp_day=""):
    """상승·하락 상위 업종(각 n_sec개)의 TOPIX500 구성 종목 중 그 방향으로 지수를 크게 움직인 종목(규모 가중 × 등락률)과 카부탄 뉴스 제목.
    → [{sector, pct, stocks: [{code, name, pct, news: [제목]}]}]"""
    import csv
    import highs
    path = os.path.join(DIR, "data", "jpx_sector.csv")
    if not secs:
        return []
    if os.path.exists(path):
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
    else:   # 아직 월간 목록 갱신 전: JPX 상장 종목 목록에서 바로 읽음
        import refresh_tickers
        refresh_tickers.jpx()
        rows = [{"code": c, "sector": sct, "size": sz} for c, sct, sz in refresh_tickers.JPX_SEC]
    ko = lambda j: highs.SECTOR_KO.get(j, highs.SECTOR_ALIAS.get(j, j))
    names = {}
    try:
        names = {r[0]: r[1] for r in csv.reader(open(os.path.join(DIR, "data", "jpx_list.csv"), encoding="utf-8"))}
    except Exception:
        pass
    want = secs[:n_sec] + [x for x in secs[::-1][:n_sec] if x not in secs[:n_sec]]
    mem = {k: [r for r in rows if ko(r["sector"]) == k] for k, _ in want}
    q = quotes(sorted({f"{r['code']}.T" for v in mem.values() for r in v}))
    out = []
    highs.JP_MOBILE = True   # GitHub 서버에서는 카부탄 PC판이 막힘
    for k, v in want:
        cand = [(SIZE_W.get(r["size"], 1) * (q.get(f"{r['code']}.T") or {}).get("chg", 0) * (1 if v >= 0 else -1), r) for r in mem.get(k, [])
                if (q.get(f"{r['code']}.T") or {}).get("chg") is not None]
        cand = [c for c in sorted(cand, key=lambda c: -c[0]) if c[0] > 0][:n_stk]
        stks = []
        for _, r in cand:
            nm = names.get(r["code"], r["code"])
            nw = stock_evidence(r["code"], jp_day)
            stks.append({"code": r["code"], "name": nm, "pct": round(q[f"{r['code']}.T"]["chg"], 2), "news": nw})
        out.append({"sector": k, "pct": v, "stocks": stks})
    return out


def jp_sector_rows(jp_day="", wrap=None):
    """33업종 등락 [(업종, %)] 상승률 순 — 전 거래일 마감 기준.
    1) 신고가 봇이 마감 때 저장한 전체 목록 2) 마감 정리의 상·하위 5 3) 장 시작 전·마감 후에만 카부탄 현재 값(장중 값이 섞이지 않게)"""
    sc = load_json("highs_state.json").get("sectors_close") or {}
    if sc.get("rows") and (not jp_day or sc.get("day") == jp_day):
        return [(k, float(v)) for k, v in sc["rows"]]
    for w in reversed(wrap or []):
        if jp_day and w.get("day") and w["day"] != jp_day:
            continue
        b = next((x for x in w.get("blocks", []) if "업종 등락 상·하위" in x[:40]), "")
        rows = [(m.group(1), float(m.group(2))) for p in re.split(r"\s·\s|[▲▼\n]", strip_html(b))
                if (m := re.fullmatch(r"\s*(\S+) ([+-]\d+(?:\.\d+)?)%\s*", p))]
        if rows:
            return sorted(dict(rows).items(), key=lambda x: -x[1])
    n = now()
    if (n.hour, n.minute) < (9, 0) or (n.hour, n.minute) >= (15, 30):
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
    "sectors": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "sector": {"type": "STRING"}, "pct": {"type": "NUMBER"}, "why": {"type": "STRING"}, "src": {"type": "STRING"},
        "stocks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "code": {"type": "STRING"}, "pct": {"type": "NUMBER"}, "why": {"type": "STRING"}, "src": {"type": "STRING"}},
            "required": ["name", "code", "pct", "why"]}}},
        "required": ["sector", "pct", "why", "stocks"]}},
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
- summary: 지수·수급·테마 흐름 4~6줄. 오늘 아침 미국 시장 흐름이 일본에 줄 영향도 1줄(추정 표시).
- sectors: [업종별 주요 종목]의 업종마다 하나씩(순서 그대로). sector(업종명), pct(업종 등락률 그대로), why(업종이 오르거나 내린 이유 1~2문장 — [마감 정리]·[뉴스]·[공시]·[미국 증시 요약] 근거, 근거가 없으면 '(추정)'),
  stocks: 그 업종에 주어진 종목마다 name(한국어 이름), code, pct(주어진 등락률 그대로), why(그 종목이 오르거나 내린 이유 1~2문장, 90자 이내).
  why는 [기사]·[시황 다이제스트 발췌]·[최근 기사 제목]·[마감 정리]·[공시]에서 구체적 재료를 찾아 써라: 실적(분기 영업이익 증감률·컨센서스 대비), 가이던스 상향·하향 폭, M&A·자사주, 증권사 목표가·등급 변경, 수주·신제품, 업황 지표 등 — 수치와 날짜가 있으면 넣어라.
  '업종 흐름 동반'은 정말 개별 재료가 전혀 없을 때만 쓰고, 근거 없이 짐작한 이유는 '(추정)'을 붙여라.
  src(업종·종목 모두): why의 근거가 된 자료를 짧게. 예: '카부탄 기사 11/2', '시황 다이제스트 10/31', '마감 정리', '공시 10/30', '뉴스봇'. 근거 없이 짐작했으면 '추정'.
- news: [뉴스] 중 투자에 중요한 기사 최대 6개. [공시]와 같은 사건을 다룬 기사는 news에서 빼고 disc에만 넣어라(중복 금지). title(한국어 제목 그대로 또는 짧게), why(왜 중요한지 1문장, 60자 이내).
- disc: [공시] 중 주목할 공시 최대 10개. i(공시 번호), why(투자 관점 한 줄, 60자 이내 — 수정 폭·지속성·시총 고려).
- watch: 오늘 일본 증시 체크 포인트 2~4줄(아래 [오늘 실적 발표 예정]·지표·이벤트, 자료에 있는 것만).
- top3: 오늘 아침 꼭 알아야 할 3줄(각 50자 이내). 1줄 미국, 1줄 일본, 1줄 오늘 주목할 것(공시·실적 발표·화제 중). 한국 관련 내용 금지.

[미국 증시 요약]
{us}

[지수] {idx}

[마감 정리(전 거래일 장 마감 후 작성)]
{wrap}

[업종별 주요 종목(업종 등락률, 그 방향으로 업종을 크게 움직인 대형·중형주와 등락률, 최근 뉴스 제목)]
{movers}

[뉴스(뉴스봇이 보낸 기사)]
{news}

[공시(공시봇 하이라이트)]
{disc}

[오늘 실적 발표 예정(시총 상위)]
{earn}
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
    fig, ax = plt.subplots(figsize=(6.2, 0.2 * len(rows) + 0.5), dpi=160)
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


LINK_RE = re.compile(r"^(?:&gt;|>)?\s*(<a .*)$")
TAG_RE = re.compile(r"^\[([^\]]{1,10})\]\s*")


def _plain(s):
    return H.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


STOCK_RE = re.compile(r"([가-힣A-Za-z0-9&·.\-]{2,24})\((\d{3}[0-9A-Z])(?: JP)?\)")


def hl_stocks(t):
    """문장 속 '종목명(코드)'를 굵게 + 코드는 밸류 데스크 링크(이미 HTML 이스케이프된 문자열에 사용)"""
    return STOCK_RE.sub(lambda m: f"<b>{m.group(1)}</b><span class='cd'>({vdl(m.group(2))})</span>", t)


def _li_html(t):
    """불릿 한 줄: 맨 앞 [분류]는 작은 꼬리표, (추정)은 흐리게"""
    t = t.strip().lstrip("•·").strip()
    tag = ""
    m = TAG_RE.match(_plain(t)) if t.startswith("[") else None
    if m:
        tag = f"<span class='tag'>{esc(m.group(1))}</span>"
        t = t[t.index("]") + 1:].strip()
    if t.startswith("(추정)"):
        return f"<li class='est'>{tag}{t}</li>"
    t = re.sub(r"^([^:<]{1,10}):\s*", r"<b class='k'>\1</b> ", t)   # '관찰:', '확인할 점:' 같은 머리말은 굵게
    m = re.match(r"^([^:<(]{1,14})\(([+-][\d.]+%)\):\s*", t)   # '항공(+5.12%): 이유' → 업종·등락 강조
    if m:
        t = f"<b>{m.group(1)}</b> <span class='{'up' if m.group(2)[0] == '+' else 'dn'}'>{m.group(2)}</span> " + t[m.end():]
    return f"<li>{tag}{hl_stocks(t)}</li>"


def pretty(b):
    """텔레그램 메시지 조각 → PDF용 구조(제목·라벨·목록·링크 줄). 줄바꿈 나열이 아니라 문서처럼"""
    out, ul, links = [], [], []
    def flush():
        if ul: out.append("<ul class='b'>" + "".join(ul) + "</ul>"); ul.clear()
    for raw in (b or "").replace("━━━━━━━━━━", "").split("\n"):
        ln = raw.strip()
        if not ln: continue
        m = LINK_RE.match(ln)
        if m and ln.lstrip("&gt; ").startswith("<a"):
            links.append(m.group(1)); continue
        if ln.startswith(("•", "·")):
            ul.append(_li_html(ln)); continue
        flush()
        lab = re.fullmatch(r"<b>\[([^\]]+)\]</b>\s*(.*)", ln)
        if lab:
            out.append(f"<div class='lbl'>{esc(lab.group(1))}</div>" + (f"<p>{lab.group(2)}</p>" if lab.group(2) else "")); continue
        tt = re.fullmatch(r"<b>(?:&gt;)?\s*(\d+\.\s*)?(.+?)</b>\s*(.*)", ln)
        if tt:
            num = f"<span class='num'>{tt.group(1).strip().rstrip('.')}</span>" if tt.group(1) else ""
            title = tt.group(2)
            tm = TAG_RE.match(_plain(title))
            if tm:
                title = f"<span class='tag'>{esc(tm.group(1))}</span>" + title[title.index("]") + 1:].strip()
            meta = re.sub(r"^[—–-]\s*", "", tt.group(3).strip())
            out.append(f"<div class='ct'>{num}{title}" + (f" <span class='meta'>{meta}</span>" if meta else "") + "</div>"); continue
        if ln.startswith("(추정)"):
            out.append(f"<p class='est'>{ln}</p>"); continue
        out.append(f"<p>{ln}</p>")
    flush()
    if links:
        out.append("<div class='lnk'>" + " · ".join(links) + "</div>")
    return "".join(out)


def _regroup(bl):
    """줄 단위로 쌓인 메시지(X·note·주간 순위): 빈 줄로 시작하는 줄에서 항목을 나눔"""
    groups, cur = [], []
    for b in bl:
        if b.startswith("\n") and cur:
            groups.append("\n".join(cur)); cur = []
        cur.append(b)
    if cur: groups.append("\n".join(cur))
    return groups


def rank_table(groups):
    """X·note·기업 순위 → 표(순위 | 주제·분류 | 지표 | 한 줄 설명)"""
    rows = []
    for g in groups:
        lines = [x.strip() for x in g.strip().split("\n") if x.strip()]
        if not lines: continue
        m = re.fullmatch(r"<b>(\d+)\.\s*(.+?)</b>\s*(.*)", lines[0])
        if not m:
            continue
        title, meta = m.group(2), re.sub(r"^[—–-]\s*|^\(|\)$", "", m.group(3).strip())
        tm = TAG_RE.match(_plain(title))
        tag = ""
        if tm:
            tag = f"<span class='tag'>{esc(tm.group(1))}</span>"; title = title[title.index("]") + 1:].strip()
        desc = " ".join(x.lstrip("•· ") for x in lines[1:] if not LINK_RE.match(x) or not x.lstrip("&gt; ").startswith("<a"))
        link = next((LINK_RE.match(x).group(1) for x in lines[1:] if LINK_RE.match(x) and x.lstrip("&gt; ").startswith("<a")), "")
        link = re.sub(r">[^<]*</a>", ">↗</a>", link) if link else ""
        rows.append(f"<tr><td class='rk'>{m.group(1)}</td><td>{tag}<b>{title}</b> {link}<div class='meta'>{meta}</div></td><td>{desc}</td></tr>")
    return "<table class='rank'><tr><th></th><th style='width:38%'>주제</th><th>내용</th></tr>" + "".join(rows) + "</table>" if rows else ""


def stock_table(blocks, gainers=False):
    """신고가·상승률 상위 종목 조각 → 표(순위 | 종목·사업 | 등락·종가 | 시총·PER·PBR | 이유)"""
    rows = []
    for b in blocks:
        lines = [x.strip() for x in b.strip().split("\n") if x.strip()]
        m = re.fullmatch(r"<b>(\d+)\.\s*(★\s*)?(.+?)\((\w{4}) JP\)</b>\s*(.*)", lines[0]) if lines else None
        if not m:
            continue
        rk, star, name, code, rest = m.groups()
        info = _plain(lines[1]) if len(lines) > 1 and not lines[1].startswith("•") else ""
        biz = next((_plain(x)[1:].strip().removeprefix("사업:").strip() for x in lines if x.startswith("•") and "사업:" in x[:6]), "")
        why = next((_plain(x)[1:].strip().removeprefix("이유:").strip() for x in lines if x.startswith("•") and "이유:" in x[:6]), "")
        pct = re.search(r"([+-]\d+(?:\.\d+)?%)", rest + " " + info)
        px = re.search(r"종가 ([\d,\.]+엔)", info)
        cap = re.search(r"시총 ([\d,\.]+(?:조|억)\s*엔)", rest + " " + info)
        per = re.search(r"PER ([\d\.,]+배)", info); pbr = re.search(r"PBR ([\d\.,]+배)", info)
        pv = pct.group(1) if pct else ""
        rows.append(
            f"<tr><td class='rk'>{rk}</td>"
            f"<td><b>{'★ ' if star else ''}{esc(_plain(name))}</b> {vdl(code)}<div class='meta'>{esc(short(biz, 46))}</div></td>"
            f"<td class='n'><span class='{cls(float(pv[:-1]) if pv else 0)}'>{pv}</span><div class='meta'>{px.group(1) if px else ''}</div></td>"
            f"<td class='n'>{cap.group(1).replace(' ', '') if cap else ''}<div class='meta'>{(per.group(1) if per else '-')} · {(pbr.group(1) if pbr else '-')}</div></td>"
            f"<td class='why'>{esc(short(why, 120))}</td></tr>")
    return ("<table class='stk'><tr><th></th><th style='width:27%'>종목</th><th>등락·종가</th><th>시총·PER·PBR</th><th>오른 이유</th></tr>"
            + "".join(rows) + "</table>") if rows else ""


def stash_html(items, title_fallback, caps=None):
    """쌓인 텔레그램 정리(커뮤니티·X·note·신고가 등) → PDF 문서 형식. caps: 종류별 최대 항목 수(잘리면 '외 n건')"""
    out = []
    for x in items:
        kind = x.get("kind")
        head_txt = re.sub(r"^&gt;&gt;|^>>", "", re.sub(r"<[^>]+>", "", x.get("head", "")).strip()).strip()
        first, _, rest_h = head_txt.partition("\n")
        title = re.sub(r"^\[([^\]]+)\]\s*", r"\1 · ", first) or title_fallback
        sub = rest_h.strip()
        bl = x.get("blocks", [])
        cap = (caps or {}).get(kind)
        if kind in ("x", "note", "weekly"):
            bl = _regroup(bl)
        more = len(bl) - cap if cap is not None and len(bl) > cap else 0
        if more: bl = bl[:cap]
        if kind in ("x", "note", "weekly"):
            body = rank_table(bl)
        elif kind in ("highs", "gainers"):
            body = stock_table(bl, kind == "gainers")
            pre = re.split(r"\n\s*\n", x.get("head", ""), maxsplit=1)
            if len(pre) > 1: body = f"<div class='intro'>{pretty(pre[1])}</div>" + body
            sub = re.sub(r"<[^>]+>", "", pre[0]).split("\n", 1)[1].strip() if "\n" in pre[0] else sub
        else:
            body = "".join(f"<div class='card'>{pretty(b)}</div>" for b in bl)
        if more: body += f"<div class='note'>… 외 {more}건 생략(분량 조절)</div>"
        out.append(f"<div class='stash'><div class='sh'>{esc(title)}</div>" + (f"<div class='shs'>{esc(sub)}</div>" if sub else "") + body + "</div>")
    return "".join(out)


PAGE_MAX = 999   # 아침 브리핑 목표 분량(쪽). 넘으면 덜 중요한 부분부터 줄임
VD_URL = "https://claude.ai/artifact/6gMrLe6Ue3ezG3HEu1ACvE"   # 밸류 데스크(종목 페이지 = VD_URL#코드)


def vdl(code):
    """종목 코드 → 밸류 데스크 종목 페이지 링크(PDF에서 누르면 열림)"""
    return f"<a class='vd' href='{VD_URL}#{esc(code)}'>{esc(code)}</a>"


CSS = """a.vd{color:#2a5bd7;text-decoration:none}

@page{size:A4;margin:14mm 12mm 14mm 12mm;@bottom-right{content:counter(page) " / " counter(pages);font:8pt NG;color:#888}
      @bottom-left{content:"아침 브리핑 · %(date)s";font:8pt NG;color:#888}}
body{font-family:NG,sans-serif;font-size:9.4pt;line-height:1.62;color:#1d2127}
h1{font-size:17pt;margin:0 0 2mm} .sub{color:#666;font-size:8.5pt}
h2{font-size:12.5pt;background:#23272e;color:#fff;padding:2mm 3.5mm;border-radius:1.5mm;margin:5mm 0 3mm;page-break-after:avoid}
h2.newpage{page-break-before:auto} h2.pb{page-break-before:always}
h3{font-size:10.6pt;margin:5mm 0 2mm;color:#23272e;border-left:3px solid #9a6a12;padding-left:2mm;page-break-after:avoid}
.hl{background:#f3e7cf;border-radius:2mm;padding:2.5mm 3.5mm;font-weight:700;font-size:10.5pt;margin:2mm 0}
ul{margin:1mm 0 2mm;padding-left:5mm} li{margin:0.9mm 0}
table{border-collapse:collapse;width:100%;font-size:8.6pt;margin:1.5mm 0 3mm}
th{background:#ece6d8;font-weight:700;text-align:left;padding:1.4mm 2mm;border-bottom:1.5px solid #c9bda3;font-size:8.4pt}
td{padding:1.4mm 2mm;border-bottom:1px solid #ebe7de;vertical-align:top} tr:nth-child(odd) td{background:#fbfaf6} tr{page-break-inside:avoid}
.cd{color:#888;font-size:8pt}
td.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.up{color:#cf2f3f} .dn{color:#2360c4}
.grid{display:flex;gap:4mm} .grid>div{flex:1}
table.disc td:first-child{width:24%} table.disc td:nth-child(2){width:42%}
img.chart{width:82%;margin:1mm 0;display:block} img.chart.sm{width:62%} img.chart.wide{width:100%}
.srct{color:#999;font-size:7pt;font-style:normal}
table.cal td{font-size:8.4pt;padding:1.2mm 2mm} td.cd0{white-space:nowrap;width:16mm;color:#9a6a12;font-weight:700}
.chg{border:1px solid #e3dccb;border-radius:2mm;padding:2.5mm 3.5mm;margin:0 0 4mm;background:#fcfbf7}
.chips{display:flex;flex-wrap:wrap;gap:1.5mm} .chip{flex:1 0 17%;border-right:1px solid #eee;padding:0.5mm 1.5mm}
.cl{font-size:7.2pt;color:#777} .cv{font-size:10pt;font-weight:700} .cc{font-size:8pt;font-weight:700}
.chev{font-size:8pt;color:#444;margin-top:1.5mm}
.stash{margin:2mm 0 5mm} .sh{font-weight:700;font-size:10.8pt;margin:3mm 0 0.5mm;color:#15181d;border-left:3px solid #9a6a12;padding-left:2mm;page-break-after:avoid}
.shs{color:#777;font-size:8pt;margin:0 0 2mm 3mm}
.blk{margin:0 0 2.5mm;page-break-inside:auto;orphans:2;widows:2}
.card{border:1px solid #e7e2d6;border-radius:2mm;background:#fcfbf7;padding:2.2mm 3.5mm;margin:0 0 2.2mm;page-break-inside:avoid}
.card p{margin:0.8mm 0}
.ct{font-weight:700;font-size:9.6pt;margin:0.5mm 0 1mm} .ct .num{display:inline-block;min-width:4.5mm;color:#9a6a12}
.meta{color:#7a7a7a;font-size:7.8pt;font-weight:400}
.lbl{display:inline-block;font-size:7.6pt;font-weight:700;color:#9a6a12;letter-spacing:0.2mm;margin:1.2mm 0 0.3mm}
ul.b{margin:0.5mm 0 1mm;padding-left:4mm} ul.b li{margin:0.4mm 0}
.est{color:#666;font-style:italic} b.k{color:#7a5410}
.tag{display:inline-block;font-size:7.2pt;font-weight:700;padding:0 1.4mm;margin-right:1.4mm;border-radius:1mm;background:#f2e8d3;color:#7a5410;font-style:normal}
.lnk{font-size:7.6pt;color:#999;margin-top:1mm} .lnk a{color:#2a5bd7}
.intro{margin:0 0 2mm} .intro p{margin:0.6mm 0}
table.rank td,table.stk td{font-size:8.3pt;padding:1.3mm 1.6mm} td.rk{color:#9a6a12;font-weight:700;width:5mm;text-align:center}
table.stk td.why{font-size:8pt;color:#333} table.rank tr,table.stk tr{page-break-inside:avoid}
.wrapg{display:flex;gap:4mm} .wrapg>div{flex:1}
a{color:#2360c4;text-decoration:none}
.note{color:#777;font-size:7.8pt;margin-top:1mm}
.toc{font-size:9pt;color:#444;margin:2mm 0 4mm}
.top3{border:1.5px solid #9a6a12;border-radius:2mm;padding:3mm 4mm;margin:2mm 0 4mm;font-size:10.2pt;line-height:1.7}
.t3h{font-weight:700;color:#9a6a12;margin-bottom:1mm}
.jpl{color:#9a6a12;font-size:8.2pt;margin-top:0.5mm}
table.watch td:last-child{font-size:8pt;color:#444}
p.earnl{font-size:8.6pt;line-height:1.75;margin:1mm 0 3mm}
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


def short(x, n):
    x = str(x or "")
    return x if len(x) <= n else x[:n - 1].rstrip() + "…"


def li(lines):
    return "<ul>" + "".join(f"<li>{hl_stocks(esc(str(x).lstrip('•·- ')))}</li>" for x in (lines or []) if str(x).strip()) + "</ul>"


def build_pdf(state, since_ts):
    t = now()
    since = datetime.fromtimestamp(since_ts, JST)
    # ── 1) 미국
    q = quotes([s for s, _ in US_IDX + MACRO + SECTORS + JP_IDX] + MEGA)
    us_day = (q.get("^GSPC") or {}).get("date", "")
    gain, lose = finviz_movers()
    ern_pre, ern_post = us_earnings()
    posts = []
    try:   # 네이버 프리미엄 버터대디 요약(맥의 요약봇이 japan stock 봇 저장소에 올려 둠) — 미국 시황 참고 글 맨 앞
        r = requests.get("https://jp-disclosure-bot.ssamgij.workers.dev/notes", timeout=20,
                         params={"since": int((t - timedelta(hours=30)).timestamp() * 1000)},
                         headers={"x-watch-key": os.environ.get("WATCH_KEY", ""), "User-Agent": "morning-brief"})
        for x in (r.json() if r.status_code == 200 else []):
            posts.append(f"### {x.get('src', '')}(네이버 프리미엄 요약) {datetime.fromtimestamp(x['ts'] / 1000, JST):%m/%d %H:%M}\n{x.get('title', '')}\n{x.get('text', '')}")
        print(f"[info] 네프콘 요약 {len(posts)}건", file=sys.stderr)
    except Exception as ex:
        print(f"[warn] 네프콘 요약 읽기 실패: {ex}", file=sys.stderr)
    for ch, label in CHANNELS:
        for ts, txt in tg_posts(ch, t - timedelta(hours=18)):
            posts.append(f"### {label} {ts:%m/%d %H:%M}\n{txt}")
    qtxt = "\n".join(filter(None, [fmt_q(q, s, n) for s, n in US_IDX + MACRO + SECTORS]))
    mega = sorted([(s, q[s]["chg"]) for s in MEGA if s in q and q[s]["chg"] is not None], key=lambda x: -x[1])
    qtxt += "\n[대형주] " + ", ".join(f"{s} {c:+.2f}%" for s, c in mega)
    ftxt = lambda rows: ", ".join(f"{r['tk']}({r['name']}, {r['ind']}) {r['pct']:+.2f}%" for r in rows if r.get("pct") is not None) or "(없음)"
    us = gemini_obj(state, US_PROMPT.format(day=us_day, quotes=qtxt, gain=ftxt(gain), lose=ftxt(lose),
                                            posts="\n\n".join(posts)[:42000] or "(없음)"), US_SCHEMA, "미국 시황", check=us_ok) or {}
    # 주목 종목 정리: 등락률이 깨진 줄은 시세 값으로 고치거나 버림
    fixed = []
    for x in us.get("stocks") or []:
        mv = str(x.get("move", "")).strip()
        tk = str(x.get("ticker", "")).strip().upper()
        if not MOVE_RE.match(mv):
            if tk in q and q[tk].get("chg") is not None:
                mv = f"{q[tk]['chg']:+.1f}%"
            else:
                m_ = re.search(r"[+-]\d+(?:\.\d+)?%", mv)
                mv = m_.group(0) if m_ else ""
        if mv and len(str(x.get("why", ""))) <= 160:
            fixed.append(dict(x, move=mv))
    us["stocks"] = fixed
    if not gain and not lose and mega:   # finviz 실패: 대형주 등락 상·하위로 대신
        gain = [{"tk": s_, "name": "", "pct": c_} for s_, c_ in mega[:8]]
        lose = [{"tk": s_, "name": "", "pct": c_} for s_, c_ in mega[::-1][:8]]
        mv_lab = "대형주"
    else:
        mv_lab = "S&P500"
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
    try:
        rates_img = rates_chart_png(usdjpy_hist(), jgb10_hist())
    except Exception as ex:
        print(f"[warn] 환율·금리 차트 실패: {ex}", file=sys.stderr); rates_img = None
    if jgb:
        jp_idx += f", 일본 국채 10년 {jgb[1]:.3f}% ({jgb[2]:+.1f}bp, {jgb[0]})"
    secs = jp_sector_rows(jp_day, wrap)
    if secs:
        jp_idx += "\n[33업종 등락 상위] " + ", ".join(f"{k} {v:+.2f}%" for k, v in secs[:5]) + " / [하위] " + ", ".join(f"{k} {v:+.2f}%" for k, v in secs[::-1][:5])
    earn, earn_n = jp_earnings_today(t.strftime("%Y-%m-%d"))
    earn_txt = ", ".join(f"{r['ko']}({r['code']}, {oku(r['cap'])}{', 관심 종목' if r['watch'] else ''})" for r in earn) or "(없음)"
    watch = watch_rows(since_ts)
    try:
        wcal = watch_calendar(t)
    except Exception as ex:
        print(f"[warn] 관심 종목 실적 캘린더 실패: {ex}", file=sys.stderr); wcal = []
    react, react_d = earnings_reaction(jp_day)
    if react_d and react_d == state.get("react_last"):  # 일본 휴장일 다음 날: 같은 실적 반응을 다시 싣지 않음
        react = []
    state["_react_new"] = react_d if react else state.get("react_last")
    pw = t - timedelta(days=1)
    while pw.weekday() >= 5:
        pw -= timedelta(days=1)
    jp_closed = bool(jp_day) and jp_day < pw.strftime("%Y-%m-%d")  # 직전 평일이 일본 휴장일
    # ── 3)·4) 쌓아 둔 메시지
    us_sum = " ".join(us.get("summary") or [])[:800] or qtxt[:600]
    try:
        movers = sector_movers(secs, jp_day=jp_day)
    except Exception as ex:
        print(f"[warn] 업종 주요 종목 실패: {ex}", file=sys.stderr); movers = []
    mv_txt = "\n".join(f"## {m['sector']} {m['pct']:+.2f}%\n" + "\n".join(
        f"- {x['name']}({x['code']}) {x['pct']:+.2f}%\n  " + ("\n  ".join(x["news"]) or "근거 자료 없음") for x in m["stocks"]) for m in movers) or "(없음)"
    jp = gemini_obj(state, JP_PROMPT.format(day=jp_day, us=us_sum, idx=jp_idx or "-", wrap=wrap_txt[:6000], movers=mv_txt[:16000], news=news_txt[:8000],
                                            disc=disc_txt[:9000], earn=earn_txt), JP_SCHEMA, "일본 시황") or {}
    hi = [x for x in wraps if x["kind"] in ("highs", "gainers")]

    # ── HTML: 분량(목표 PAGE_MAX쪽)에 맞춰 단계(lv)별로 덜 중요한 부분부터 줄임
    def assemble(lv, ul=0):
        wd = "월화수목금토일"[t.weekday()]
        parts = [f"<h1>아침 브리핑 {t:%Y-%m-%d}({wd})</h1>",
                 f"<div class='sub'>{t:%H:%M} KST 작성 · 대상 기간 {since:%m/%d %H:%M} ~ {t:%m/%d %H:%M} · 미국 {us_day or '-'} 종가 · 일본 {jp_day or '-'} 종가</div>"]
        top3 = [x for x in (jp.get("top3") or []) if str(x).strip()][:3] or [x for x in (us.get("headline"), jp.get("headline")) if x]
        if top3:
            parts.append("<div class='top3'><div class='t3h'>오늘의 3줄 요약</div>" + "".join(f"<div>{i}. {esc(str(x).lstrip('•·- 0123456789.'))}</div>" for i, x in enumerate(top3, 1)) + "</div>")
        chips = []
        def chip(lab, x, kind="pct"):
            if not x or x.get("px") is None or (kind == "pct" and x.get("chg") is None):
                return
            if kind == "bp":
                val, ch, c_ = f"{x['px']:.2f}%", f"{x['d'] * 100:+.0f}bp", cls(x["d"])
            elif kind == "fx":
                val, ch, c_ = f"{x['px']:.2f}", f"{x['d']:+.2f}", cls(x["d"])
            else:
                val, ch, c_ = f"{x['px']:,.0f}" if x["px"] > 1000 else f"{x['px']:,.2f}", f"{x['chg']:+.2f}%", cls(x["chg"])
            chips.append(f"<div class='chip'><div class='cl'>{lab}</div><div class='cv'>{val}</div><div class='cc {c_}'>{ch}</div></div>")
        chip("닛케이225", q.get("^N225")); chip("TOPIX(ETF)", q.get("1306.T")); chip("달러/엔", q.get("JPY=X"), "fx")
        if jgb:
            chips.append(f"<div class='chip'><div class='cl'>일본 국채 10년</div><div class='cv'>{jgb[1]:.3f}%</div><div class='cc {cls(jgb[2])}'>{jgb[2]:+.1f}bp</div></div>")
        chip("S&P500", q.get("^GSPC")); chip("나스닥", q.get("^IXIC")); chip("반도체(SOX)", q.get("^SOX")); chip("미 국채 10년", q.get("^TNX"), "bp"); chip("WTI", q.get("CL=F"))
        ev = [x for x in ((jp.get("watch") or [])[:1] + (us.get("watch") or [])[:1]) if str(x).strip()]
        if chips:
            parts.append("<div class='chg'><div class='t3h'>전일 대비 무엇이 바뀌었나</div><div class='chips'>" + "".join(chips) + "</div>"
                         + (f"<div class='chev'>오늘 체크: {' / '.join(esc(str(x).lstrip('•·- ')) for x in ev)}</div>" if ev else "") + "</div>")
        # 1
        parts.append("<h2>1. 글로벌 시황 (미국 중심)</h2>")
        if us.get("headline"):
            parts.append(f"<div class='hl'>{esc(us['headline'])}</div>")
        parts.append("<div class='grid'><div><h3>지수</h3>" + q_table(q, US_IDX) + "</div><div><h3>금리·환율·원자재</h3>"
                     + q_table(q, MACRO) + "</div></div>")
        if us.get("summary"):
            parts.append("<h3>시장 흐름</h3>" + li(us["summary"][:(9, 9, 7, 6, 5, 4)[ul]]))
        sec_rows = [(n, q[s]["chg"]) for s, n in SECTORS if s in q and q[s]["chg"] is not None]
        if sec_rows and ul < 4:
            try:
                parts.append(f"<img class='chart{' sm' if ul >= 2 else ''}' src='{chart_png(sec_rows, 'S&P500 섹터 ETF 등락률(%)')}'>")
            except Exception as ex:
                print(f"[warn] 섹터 차트 실패: {ex}", file=sys.stderr)
        if us.get("sectors"):
            parts.append("<h3>섹터</h3>" + li(us["sectors"]))
        if us.get("stocks"):
            parts.append("<h3>주목 종목</h3><table><tr><th>종목</th><th>등락</th><th>내용</th></tr>" + "".join(
                f"<tr><td>{esc(s.get('name', ''))} <span class='note'>{esc(s.get('ticker', ''))}</span></td>"
                f"<td class='n {cls(-1 if str(s.get('move', '')).startswith('-') else 1)}'>{esc(s.get('move', ''))}</td><td>{esc(s.get('why', ''))}"
                + (f"<div class='jpl'>🇯🇵 {esc(s['jp'].lstrip('→ '))}</div>" if (s.get('jp') or '').strip() else "") + "</td></tr>"
                for s in us["stocks"][:(10, 8, 7, 6, 5, 4)[ul]]) + "</table>")
        if (gain or lose) and ul < 3:
            mk = lambda rows: "".join(f"<tr><td>{esc(r['tk'])} <span class='note'>{esc(r['name'][:28])}</span></td><td class='n {cls(r['pct'])}'>{r['pct']:+.2f}%</td></tr>"
                                      for r in rows[:(8, 6, 5)[ul]] if r.get("pct") is not None)
            parts.append(f"<div class='grid'><div><h3>{mv_lab} 상승 상위</h3><table>" + mk(gain) + f"</table></div><div><h3>{mv_lab} 하락 상위</h3><table>"
                         + mk(lose) + "</table></div></div>")
        if (ern_pre or ern_post) and ul < 2:
            ek = lambda rows: "".join(f"<tr><td>{esc(r['tk'])} <span class='note'>{esc(r['name'][:26])}</span></td><td class='n'>{esc(r['cap'])}</td></tr>" for r in rows) or "<tr><td class='note'>없음</td><td></td></tr>"
            parts.append("<h3>오늘 밤 실적 발표(미국, 시총 100억 달러↑)</h3><div class='grid'><div><table><tr><th>장 전</th><th>시총($B)</th></tr>" + ek(ern_pre)
                         + "</table></div><div><table><tr><th>장 마감 후</th><th>시총($B)</th></tr>" + ek(ern_post) + "</table></div></div>")
        if us.get("macro"):
            parts.append("<h3>매크로</h3>" + li(us["macro"][:(9, 9, 4, 3, 3, 2)[ul]]))
        if us.get("watch"):
            parts.append("<h3>체크 포인트</h3>" + li(us["watch"][:(9, 9, 4, 3, 3, 2)[ul]]))
        if not us:
            parts.append("<p class='note'>(AI 정리 실패: 시세 표만 싣습니다)</p>")
        # 2
        parts.append("<!--JP--><h2 class='pb'>2. 일본 증시 시황</h2>")
        if jp.get("headline"):
            parts.append(f"<div class='hl'>{esc(jp['headline'])}</div>")
        jt = q_table(q, JP_IDX)
        if jgb and jt:
            jt = jt.replace("</table>", f"<tr><td>일본 국채 10년 <span class='note'>{esc(jgb[0])}</span></td><td class='n'>{jgb[1]:.3f}%</td><td class='n {cls(jgb[2])}'>{jgb[2]:+.1f}bp</td></tr></table>")
        if jp_closed:
            parts.append(f"<p class='note'>※ {pw:%m/%d}({'월화수목금토일'[pw.weekday()]})은 일본 휴장일 — 일본 지수·업종·실적 반응은 마지막 거래일 {jp_day[5:].replace('-', '/')} 기준(이미 실은 내용은 생략).</p>")
        parts.append(jt)
        if rates_img:
            parts.append(f"<img class='chart wide' src='{rates_img}'>")
        if jp.get("summary"):
            parts.append("<h3>시장 흐름</h3>" + li(jp["summary"]))
        if secs:
            try:
                k = min(8 if lv < 2 else 5, max(1, len(secs) // 2))
                pick = secs[:k] + [x for x in secs[-k:] if x not in secs[:k]]
                parts.append(f"<img class='chart' src='{chart_png(pick, f'33업종 등락률 상·하위 {k}(%) · 전 거래일')}'>")
            except Exception as ex:
                print(f"[warn] 업종 차트 실패: {ex}", file=sys.stderr)
        wb = wrap[-1]["blocks"][:-1] if wrap else []
        sec_b = [b for b in wb if re.match(r"<b>\[(오른|내린) 업종\]</b>", b.strip())]
        jsec = [x for x in (jp.get("sectors") or []) if isinstance(x, dict) and x.get("sector")]
        if jsec:   # 업종 이유 + 그 업종을 움직인 종목과 종목별 이유
            def srct(x):
                s_ = str(x.get("src") or "").strip()
                return f" <span class='srct'>[{esc(s_)}]</span>" if s_ else ""

            def sbox(x):
                p = x.get("pct") or 0
                h = (f"<div class='card'><div class='ct'>{esc(x['sector'])} <span class='{cls(p)}'>{p:+.2f}%</span></div>"
                     f"<p>{esc(x.get('why', ''))}{srct(x)}</p><ul class='b'>")
                for st in (x.get("stocks") or [])[:3]:
                    sp = st.get("pct") or 0
                    h += (f"<li><b>{esc(st.get('name', ''))}</b> {vdl(str(st.get('code', '')))} <span class='{cls(sp)}'>{sp:+.2f}%</span>"
                          f" — <span class='{'est' if str(st.get('why', '')).startswith('(추정)') else ''}'>{esc(st.get('why', ''))}</span>{srct(st)}</li>")
                return h + "</ul></div>"
            ups = [x for x in jsec if (x.get("pct") or 0) >= 0]; dns = [x for x in jsec if (x.get("pct") or 0) < 0]
            parts.append("<h3>업종 등락과 이유</h3><div class='wrapg'><div><div class='lbl'>오른 업종</div>" + "".join(sbox(x) for x in ups)
                         + "</div><div><div class='lbl'>내린 업종</div>" + "".join(sbox(x) for x in dns) + "</div></div>")
        elif len(sec_b) == 2:   # AI 실패: 마감 정리(카부탄·피스코 마감 시황 기반)의 업종별 이유를 좌우로
            parts.append("<h3>업종 등락과 이유</h3><div class='wrapg'>" + "".join(f"<div>{pretty(b)}</div>" for b in sec_b) + "</div>")
        rest_b = [b for b in wb if b not in sec_b and "업종 등락 상·하위" not in b[:40]]
        if rest_b:
            parts.append("<h3>전 거래일 마감 정리</h3>" + "".join(f"<div class='card'>{pretty(b)}</div>" for b in rest_b))
        if not jp.get("news") and news:  # AI 실패: 뉴스봇이 보낸 기사 상위 8개 그대로
            jp["news"] = [{"title": r.get("ko", ""), "why": r.get("gist", "")[:80]} for r in news[-8:]]
        if jp.get("news"):
            parts.append("<h3>주목 뉴스</h3><table><tr><th>기사</th><th>왜 중요한가</th></tr>" + "".join(
                f"<tr><td><b>{esc(n.get('title', ''))}</b></td><td>{hl_stocks(esc(n.get('why', '')))}</td></tr>" for n in jp["news"][:(8, 6, 5, 5, 4, 3)[lv]]) + "</table>")
        if disc:
            picks = [(d["i"] - 1, d.get("why", "")) for d in (jp.get("disc") or []) if isinstance(d.get("i"), int) and 0 < d["i"] <= len(disc)]
            if not picks:
                picks = [(i, disc[i].get("why") or disc[i].get("why0") or "") for i in range(min(10, len(disc)))]
            wset = {r["code"] for r in watch}  # 관심 종목 표에 이미 나오는 공시는 빼서 중복 줄임
            picks = [(i, w) for i, w in picks if disc[i]["code"] not in wset] or picks
            picks = picks[:(10, 10, 8, 7, 6, 5)[lv]]
            parts.append("<h3>주목 공시</h3><table class='disc'><tr><th>기업</th><th>내용</th><th>포인트</th></tr>" + "".join(
                f"<tr><td><b>{esc(disc[i]['name'])}</b> <span class='note'>{vdl(disc[i]['code'])} · {esc(disc[i].get('mct') or '')}</span></td>"
                f"<td>{esc(disc[i]['txt'])}</td><td>{esc(short(why, 110))}</td></tr>" for i, why in picks) + "</table>")
        if react:
            def rrow(r):
                tag = ("⭐ " if r["watch"] else "") + esc(r["ko"])
                info = " · ".join(filter(None, [("스크리닝 통과" + (f"({r['tag']})" if r["tag"] else "")) if r["pass"] else "", r["txt"]]))
                return (f"<tr><td>{tag} <span class='note'>{vdl(r['code'])}</span></td><td class='n'>{oku(r['cap'])}</td>"
                        f"<td class='n {cls(r['chg'])}'>{r['chg']:+.2f}%</td><td>{esc(info[:150])}</td></tr>")
            parts.append(f"<h3>실적 발표 다음 날 주가 반응 <span class='note'>{react_d[4:6]}/{react_d[6:]} 발표 → {jp_day[5:].replace('-', '/')} 등락 · 시총 300억엔↑·관심 종목, 상승·하락 각 8</span></h3>"
                         "<table><tr><th>종목</th><th>시총</th><th>등락</th><th>실적 내용</th></tr>" + "".join(rrow(r) for r in react[:(16, 16, 12, 10, 8, 6)[lv]]) + "</table>")
        if wcal:
            parts.append("<h3>관심 종목 실적 발표 (앞으로 7일)</h3><table class='cal'>" + "".join(
                f"<tr><td class='cd0'>{d[5:].replace('-', '/')}({'월화수목금토일'[datetime.strptime(d, '%Y-%m-%d').weekday()]})</td><td>"
                + " · ".join(f"<b>{esc(n)}</b> {vdl(c)}" for c, n in rows) + "</td></tr>" for d, rows in wcal) + "</table>")
        if watch:
            def wrow(r):
                px = f"{r['px']:,.0f}" if r["px"] else "-"
                ch = f"{r['chg']:+.2f}%" if r["chg"] is not None else "-"
                gap = f"{r['gap']:+.1f}%" if r["gap"] is not None else "-"
                nh = " <b class='up'>신고가</b>" if r["nh"] else ""
                return (f"<tr><td>{esc(r['ko'])} <span class='note'>{vdl(r['code'])}</span>{nh}</td><td class='n'>{px}</td>"
                        f"<td class='n {cls(r['chg'])}'>{ch}</td><td class='n'>{gap}</td><td>{esc(' / '.join(r['disc'])[:160])}</td></tr>")
            parts.append("<h3>관심 종목 <span class='note'>전 거래일 등락 큰 순 · 기간 중 공시</span></h3><table class='watch'><tr><th>종목</th><th>종가</th>"
                         "<th>등락</th><th>52주 고가 대비</th><th>공시</th></tr>" + "".join(wrow(r) for r in watch[:(99, 30, 20, 15, 12, 10)[lv]]) + "</table>")
        if jp.get("watch"):
            parts.append("<h3>체크 포인트</h3>" + li(jp["watch"]))
        # 3
        parts.append("<h2>3. 일본 52주 신고가 · 상승률 상위</h2>")
        hd = max((x.get("day", "") for x in hi), default="")
        hs = [x for x in hi if lv == 0 or x.get("day", "") == hd]   # 줄일 때는 마지막 거래일 것만
        hcaps = [{"highs": 40, "gainers": 18}, {"highs": 25, "gainers": 12}, {"highs": 18, "gainers": 10},
                 {"highs": 12, "gainers": 8}, {"highs": 8, "gainers": 6}, {"highs": 6, "gainers": 5}][lv]
        parts.append(stash_html(sorted(hs, key=lambda x: (x.get("day", ""), x["kind"] != "highs")), "신고가", hcaps)
                     if hi else "<p class='note'>이 기간에 쌓인 신고가·상승률 정리가 없습니다(휴장일이거나 아직 발송 전).</p>")
        doc = (f"<html><head><meta charset='utf-8'><style>{font_css()}{CSS.replace('%(date)s', t.strftime('%Y-%m-%d'))}</style></head><body>"
               + "".join(parts) + "</body></html>")
        return doc, top3

    from weasyprint import HTML
    ul = 0
    for ul in range(6):   # 미국(1장)은 2쪽 안에: 넘치면 미국 부분만 단계별로 줄임
        d0, _ = assemble(0, ul)
        n_us = len(HTML(string=d0.split("<!--JP-->")[0] + "</body></html>", base_url=DIR).render().pages)
        print(f"[info] 미국 단계 {ul}: {n_us}쪽", file=sys.stderr)
        if n_us <= 2: break
    for lv in range(6):
        doc, top3 = assemble(lv, ul)
        rd = HTML(string=doc, base_url=DIR).render()
        print(f"[info] 분량 단계 {lv}: {len(rd.pages)}쪽", file=sys.stderr)
        if len(rd.pages) <= PAGE_MAX: break
    pdf = rd.write_pdf()
    wd = "월화수목금토일"[t.weekday()]
    cap = [f"<b>아침 브리핑 {t:%m/%d}({wd})</b>"]
    if top3:
        cap += [f"{i}. {esc(str(x).lstrip('•·- 0123456789.'))}" for i, x in enumerate(top3, 1)]
    cap.append(f"신고가·상승률 {len(hi)}건")
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
    if FORCE:   # 테스트 발송: 마지막 발송 이후가 아니라 최근 26시간(MORNING_HOURS로 바꿀 수 있음) — 평소 브리핑과 같은 분량으로 확인
        since_ts = time.time() - float(os.getenv("MORNING_HOURS") or (74 if now().weekday() == 0 else 26)) * 3600   # 월요일은 주말 포함
    t_start = time.time()
    pdf, cap = build_pdf(st, since_ts)
    print(f"[info] PDF {len(pdf) / 1024:.0f}KB, {time.time() - t_start:.0f}초")
    if wait and not FORCE:
        target = now().replace(hour=SEND_AT[0], minute=SEND_AT[1], second=0, microsecond=0)
        sec = (target - now()).total_seconds()
        if sec > 0:
            print(f"[info] {sec / 60:.0f}분 대기 후 발송"); time.sleep(sec)
    if send_pdf(pdf, cap, f"아침브리핑_{today}.pdf"):
        new_react = st.pop("_react_new", None)
        if not FORCE:  # 테스트 발송(MORNING_NOW)은 기간 기준을 바꾸지 않음
            st["last_ts"] = t_start
            st["sent"] = today
            if new_react:
                st["react_last"] = new_react
        if not DRY_RUN:
            save_state(st)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
