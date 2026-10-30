"""장 마감 후 52주 신고가 종목 정리 → 텔레그램(뉴스봇 채널). 일본·한국·미국.

일본: 카부탄 '本日、52週高値を更新した銘柄'(16:00 확정 목록) + 종목 페이지(시총·개요·업종·테마) + 종목 뉴스·공시 제목.
한국: 네이버 증권 시가총액 목록(코스피·코스닥)에서 당일 상승 종목을 추린 뒤, 일봉 1년치로 '오늘 고가 > 직전 52주 최고가'를 직접 판정.
      회사 개요는 와이즈리포트(네이버 기업정보), 재료는 네이버 종목 뉴스·공시 제목.
미국: finviz 'New High'(52주 신고가) 스크리너(주식만, 시총 순) + 종목 뉴스 제목.
공통: 시가총액 상위 30종목은 사업·신고가 이유까지, 나머지는 이름·코드만. 이유는 뉴스·공시 제목 근거가 있을 때만 사실로 쓰고,
      없으면 '(추정)'. 구글 뉴스(최근 3일) 제목도 보조로 쓴다.
실행: python highs.py JP|KR|US|auto  (auto = 현재 시각으로 판단: 16~23시 → JP·KR, 05~12시 → US)
상태: highs_state.json(시장별 마지막 발송일, 같은 날 두 번 보내지 않음). 뉴스봇(bot.py)의 Gemini·텔레그램 함수를 재사용.
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY
선택: TELEGRAM_ADMIN_CHAT_ID, HIGHS_TOP_N(30), HIGHS_NOW=1(오늘 이미 보냈어도·시각 전이어도 다시 보냄), DRY_RUN=1
"""
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser
import requests

import bot
from bot import JST, UA, esc

bot.RUN_BUDGET = int(os.getenv("RUN_BUDGET", "1300"))  # 일본·한국을 한 번에 돌 수 있어 넉넉히(워크플로우 제한 25분)
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "highs_state.json")
TOP_N = int(os.getenv("HIGHS_TOP_N", "30"))
OTHERS_MAX = 120                       # '그 밖의 신고가'에 이름을 적는 최대 수
FORCE = os.getenv("HIGHS_NOW") == "1"


def now_in(tz_name):
    if tz_name == "ET":  # 미국 동부(서머타임: 3월 둘째 일요일 ~ 11월 첫째 일요일)
        u = datetime.now(timezone.utc)
        y = u.year
        mar = datetime(y, 3, 8, 7, tzinfo=timezone.utc)
        dst_start = mar + timedelta(days=(6 - mar.weekday()) % 7)
        nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)
        dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
        return u.astimezone(timezone(timedelta(hours=-4 if dst_start <= u < dst_end else -5)))
    return datetime.now(JST)  # 일본·한국은 같은 UTC+9


# ───────────────────────── 상태 ─────────────────────────
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("last", {})
    s.setdefault("fail", {})
    s.setdefault("model", {"names": [], "checked": 0})
    return s


def save_state(s):
    s["pdfq"] = [x for x in s.get("pdfq", []) if time.time() - x["ts"] < 8 * 86400]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=0)


# ───────────────────────── 공통 ─────────────────────────
def get(url, enc="utf-8", **kw):
    last = None
    for wait in (0, 5, 15):
        time.sleep(wait)
        try:
            r = requests.get(url, headers=UA, timeout=20, **kw)
            if r.status_code == 200:
                r.encoding = enc
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as ex:
            last = str(ex)
    raise RuntimeError(f"{url}: {last}")


def text_of(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def num(s):
    s = text_of(str(s)).replace(",", "").replace("+", "").replace("％", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


NOISE = re.compile(r"52週|年初来高値|上場来高値|新高値|52-week high|all-time high|신고가|목표주가|目標株価|price target", re.I)


def google_news(q, params, days=7, n=5):
    """최근 days일 구글 뉴스 제목. '신고가 경신'·'목표가' 같은 결과성 제목은 뒤로 돌린다(사업 동인이 담긴 제목 우선)."""
    try:
        f = feedparser.parse(requests.get(
            f"https://news.google.com/rss/search?{params}&q={urllib.parse.quote(f'{q} when:{days}d')}",
            headers=UA, timeout=20).content)
        titles = [bot.SUFFIX_RE.sub("", html.unescape(e.title)).strip()[:110] for e in f.entries[:12]]
        titles.sort(key=lambda t: bool(NOISE.search(t)))
        return ["[구글뉴스] " + t for t in titles[:n]]
    except Exception:
        return []


def recent(ts, now, days=10):
    return now - ts <= timedelta(days=days)


# ───────────────────────── 일본(카부탄) ─────────────────────────
JP_LIST = "https://kabutan.jp/warning/record_w52_high_price"
JP_NON_STOCK = {"東Ｅ", "東Ｒ", "東Ｉ"}  # ETF·ETN, 리츠, 인프라펀드
JP_GENERIC = re.compile(r"^本日の【|^【[^】]*】(引け|前引け|寄り|前場|後場)|銘柄$|^＜注目銘柄＞|ランキング")
SECTOR_KO = {
    "水産・農林業": "수산·농림", "鉱業": "광업", "建設業": "건설", "食料品": "식품", "繊維製品": "섬유",
    "パルプ・紙": "펄프·종이", "化学": "화학", "医薬品": "의약품", "石油・石炭製品": "석유·석탄", "ゴム製品": "고무",
    "ガラス・土石製品": "유리·토석", "鉄鋼": "철강", "非鉄金属": "비철금속", "金属製品": "금속제품", "機械": "기계",
    "電気機器": "전기기기", "輸送用機器": "수송용기기", "精密機器": "정밀기기", "その他製品": "기타제품",
    "電気・ガス業": "전기·가스", "陸運業": "육운", "海運業": "해운", "空運業": "항공", "倉庫・運輸関連業": "창고·운수",
    "情報・通信業": "정보·통신", "卸売業": "도매", "小売業": "소매", "銀行業": "은행", "証券、商品先物取引業": "증권",
    "証券・商品先物取引業": "증권", "保険業": "보험", "その他金融業": "기타금융", "不動産業": "부동산", "サービス業": "서비스",
}


JP_MOBILE = False  # PC판 카부탄이 막히면(GitHub 서버에서 HTTP 405 등) 모바일판(s.kabutan.jp)으로 전환


def jp_list(today):
    global JP_MOBILE
    try:
        return _jp_list_pc(today)
    except Exception as ex:
        print(f"[warn] 카부탄 PC판 실패 → 모바일판으로 전환: {ex}", file=sys.stderr)
    JP_MOBILE = True
    return _jp_list_mobile(today)


def _jp_mkt(s):
    return unicodedata.normalize("NFKC", text_of(s))


def _jp_list_mobile(today):
    rows, seen, day, asof = [], set(), "", ""
    for page in range(1, 60):
        h = get("https://s.kabutan.jp/warnings/record_w52_high_price/", params={"page": page})
        if page == 1:
            m = re.search(r"株価：(\d+)年(\d+)月(\d+)日\s*([\d:]+)", h)
            if not m:
                raise RuntimeError("카부탄 모바일 목록 기준일을 찾지 못함(페이지 구조 변경 가능성)")
            day = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            asof = m.group(4)
            if day != today and not FORCE:
                return day, asof, None
        new = 0
        for tr in re.findall(r"<tr>(.*?)</tr>", h, re.S):
            m = re.search(r'href="/stocks/(\w+)/".*?<p class="font-bold[^"]*">(.*?)</p>.*?(\w+)\s*<span class="px-1[^"]*">(.*?)</span>',
                          tr, re.S)
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if not m or m.group(1) in seen or len(tds) < 6:
                continue
            seen.add(m.group(1))
            new += 1
            mkt = _jp_mkt(m.group(4))
            if mkt in ("東E", "東R", "東I"):
                continue
            pct = re.findall(r"([+-]?[\d.]+)</span><span[^>]*>%", tds[1])
            per, pbr = text_of(tds[3]).replace("倍", "").strip(), text_of(tds[4]).replace("倍", "").strip()
            rows.append({"code": m.group(1), "name": text_of(m.group(2)), "mkt": mkt, "price": text_of(tds[0]) + "엔",
                         "pct": num(pct[0]) if pct else None,
                         "val": " · ".join(x for x in (f"PER {per}배" if num(per) else "", f"PBR {pbr}배" if num(pbr) else "") if x)})
        if not new:
            break
        time.sleep(0.7)
    return day, f"카부탄 {asof} 기준, ETF·리츠 제외", rows


def _jp_list_pc(today):
    rows, seen, day, asof = [], set(), "", ""
    for page in range(1, 60):
        h = get(JP_LIST, params={"market": 0, "capitalization": -1, "stc": "code", "stm": 0, "page": page})
        if page == 1:
            m = re.search(r'<div class="meigara_count">\s*<ul>\s*<li>(\d+)年(\d+)月(\d+)日</li>\s*<li>([^<]*)</li>', h)
            if not m:
                raise RuntimeError("카부탄 목록 기준일을 찾지 못함(페이지 구조 변경 가능성)")
            day = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            asof = m.group(4).strip()
            if day != today and not FORCE:
                return day, asof, None
        body = h[h.find('<table class="stock_table'):]
        body = body[:body.find("</table>")]
        new = 0
        for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S):
            code = re.search(r'<td class="tac"><a href="/stock/\?code=(\w+)">', tr)
            tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
            if not code or code.group(1) in seen or len(tds) < 13:
                continue
            seen.add(code.group(1))
            new += 1
            if text_of(tds[2]) in JP_NON_STOCK:
                continue
            per, pbr = text_of(tds[10]), text_of(tds[11])
            rows.append({"code": code.group(1), "name": text_of(tds[1]), "mkt": text_of(tds[2]),
                         "price": text_of(tds[5]) + "엔", "pct": num(tds[8]),
                         "val": " · ".join(x for x in (f"PER {per}배" if num(per) else "", f"PBR {pbr}배" if num(pbr) else "") if x)})
        if not new:
            break
        time.sleep(0.7)
    return day, f"카부탄 {asof.replace('現在', '')} 기준, ETF·리츠 제외", rows


def jp_cap(h):
    m = re.search(r'class="v_zika2">(.*?)</td>', h, re.S)
    m2 = re.match(r"(?:(\d+)兆)?([\d.]+)?億円", text_of(m.group(1)).replace(",", "").replace(" ", "")) if m else None
    return int(m2.group(1) or 0) * 10000 + float(m2.group(2) or 0) if m2 else None


def jp_profile(r):
    if JP_MOBILE:
        h = get(f"https://s.kabutan.jp/stocks/{r['code']}/")
        m = re.search(r"時価総額</div>.*?>\s*(?:([\d,]+)兆)?([\d,.]+)?\s*<span[^>]*>億円", h, re.S)
        r["cap"] = (int((m.group(1) or "0").replace(",", "")) * 10000 + float((m.group(2) or "0").replace(",", ""))) if m else None
        g = re.search(r"業種\s*</span>\s*<a[^>]*>(.*?)</a>", h, re.S)
        g = text_of(g.group(1)) if g else ""
        r["sector"] = SECTOR_KO.get(g, g or "기타")
        o = re.search(r"概要\s*</div>\s*<div[^>]*>(.*?)</div>", h, re.S)
        r["profile"] = text_of(o.group(1)) if o else ""
        return
    h = get(f"https://kabutan.jp/stock/?code={r['code']}")
    cap = jp_cap(h)
    r["cap"] = cap  # 억엔

    def row(label):
        m = re.search(r"<th scope='row'>%s</th>\s*<td>(.*?)</td>" % label, h, re.S)
        return m.group(1) if m else ""
    g = text_of(row("業種"))
    r["sector"] = SECTOR_KO.get(g, g or "기타")
    r["profile"] = text_of(row("概要")) + " / 테마: " + ", ".join(
        text_of(x) for x in re.findall(r"<li><a[^>]*>(.*?)</a></li>", row("テーマ"))[:6])


def jp_news(r, now):
    out = []
    if JP_MOBILE:
        try:
            h = get(f"https://s.kabutan.jp/stocks/{r['code']}/news/")
            for li in re.findall(r"<li[^>]*>(.*?)</li>", h, re.S):
                t = re.search(r'datetime="(\d{4}-\d\d-\d\d \d\d:\d\d)', li)
                title = re.search(r'<div class="w-full[^"]*">(.*?)</div>', li, re.S)
                ctg = re.search(r'<span class="news_category[^"]*">(.*?)</span>', li, re.S)
                if not t or not title:
                    continue
                ts, title = datetime.strptime(t.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=JST), text_of(title.group(1))
                if recent(ts, now) and not JP_GENERIC.search(title):
                    out.append(f"{ts.strftime('%m/%d')} [{text_of(ctg.group(1)) if ctg else ''}] {title[:100]}")
                if len(out) >= 8:
                    break
        except Exception as ex:
            print(f"[warn] 카부탄 모바일 뉴스 실패 {r['code']}: {ex}", file=sys.stderr)
        return out + google_news(r["name"], "hl=ja&gl=JP&ceid=JP:ja")
    try:
        h = get(f"https://kabutan.jp/stock/news?code={r['code']}")
        body = h[h.find('<table class="s_news_list'):]
        for tr in re.findall(r"<tr>(.*?)</tr>", body[:body.find("</table>")], re.S):
            t = re.search(r'datetime="([^"]+)"', tr)
            ctg = re.search(r'class="newslist_ctg[^"]*">([^<]*)<', tr)
            a = re.findall(r"<a [^>]*>(.*?)</a>", tr, re.S)
            if not t or not a:
                continue
            ts, title = datetime.fromisoformat(t.group(1)), text_of(a[-1])
            if recent(ts, now) and not JP_GENERIC.search(title):
                out.append(f"{ts.strftime('%m/%d')} [{text_of(ctg.group(1)) if ctg else ''}] {title[:100]}")
            if len(out) >= 8:
                break
    except Exception as ex:
        print(f"[warn] 카부탄 뉴스 실패 {r['code']}: {ex}", file=sys.stderr)
    return out + google_news(r["name"], "hl=ja&gl=JP&ceid=JP:ja")


# ───────────────────────── 한국(네이버) ─────────────────────────
def kr_list(today):
    allst = []
    for mkt in ("KOSPI", "KOSDAQ"):
        for page in range(1, 40):
            d = json.loads(get(f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}",
                               params={"page": page, "pageSize": 100}))
            st = d.get("stocks") or []
            for s in st:
                if s.get("stockEndType") != "stock" or "스팩" in (s.get("stockName") or ""):
                    continue
                allst.append({"code": s["itemCode"], "name": s["stockName"], "mkt": "KS" if mkt == "KOSPI" else "KQ",
                              "close": num(s.get("closePrice")), "pct": num(s.get("fluctuationsRatio")),
                              "cap": num(s.get("marketValue")), "price": (s.get("closePrice") or "") + "원", "val": ""})
            if len(st) < 100:
                break
            time.sleep(0.3)
    if not allst:
        raise RuntimeError("네이버 시가총액 목록 0건")
    cands = [s for s in allst if (s["pct"] or 0) > 0]  # 신고가 경신일은 사실상 상승 마감(하락 마감 신고가는 놓칠 수 있음)

    def check(s):
        try:
            h = get("https://fchart.stock.naver.com/sise.nhn",
                    params={"symbol": s["code"], "timeframe": "day", "count": 260, "requestType": 0}, enc="euc-kr")
        except Exception:
            return None, None
        bars = [x.split("|") for x in re.findall(r'<item data="([^"]+)"', h)]
        if len(bars) < 60:
            return None, bars[-1][0] if bars else None  # 상장 3개월 미만은 제외
        last = bars[-1]
        cut = (datetime.strptime(last[0], "%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")
        prev = [float(b[2]) for b in bars[:-1] if b[0] >= cut and b[2] not in ("", "0")]
        return (bool(prev) and float(last[2]) > max(prev)), last[0]

    rows, days = [], Counter()
    with ThreadPoolExecutor(20) as ex:
        for s, (hit, d) in zip(cands, ex.map(check, cands)):
            if d:
                days[d] += 1
            if hit and d == today.replace("-", ""):
                rows.append(s)
    day = days.most_common(1)[0][0] if days else ""
    day = f"{day[:4]}-{day[4:6]}-{day[6:]}" if day else ""
    if day != today and not FORCE:
        return day, "", None
    return day, f"네이버 일봉 기준, 당일 상승 {len(cands)}종목 중 판정", rows


def kr_profile(r):
    try:
        h = get(f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={r['code']}")
        m = re.search(r'class="cmp_comment">(.*?)</', h, re.S) or re.search(r"dot_cmp[^>]*>(.*?)</", h, re.S)
        w = re.search(r"WICS\s*:\s*([^<&]+)", h)
        r["sector"] = text_of(w.group(1)) if w else "기타"
        r["profile"] = text_of(m.group(1))[:300] if m else ""
    except Exception as ex:
        print(f"[warn] 와이즈리포트 실패 {r['code']}: {ex}", file=sys.stderr)
        r.update(sector="기타", profile="")
    try:
        d = json.loads(get(f"https://m.stock.naver.com/api/stock/{r['code']}/integration"))
        info = {x.get("code"): x.get("value") for x in d.get("totalInfos") or []}
        r["val"] = " · ".join(f"{k} {info[c]}" for k, c in (("PER", "per"), ("PBR", "pbr"))
                              if info.get(c) and info[c] not in ("N/A", "-"))
    except Exception:
        pass


def kr_news(r, now):
    out = []
    try:
        for x in json.loads(get(f"https://m.stock.naver.com/api/stock/{r['code']}/disclosure",
                                params={"pageSize": 10, "page": 1})) or []:
            ts = datetime.fromisoformat(x["datetime"]).replace(tzinfo=JST)
            if recent(ts, now) and "가격제한폭" not in x.get("title", ""):
                out.append(f"{ts.strftime('%m/%d')} [공시] {x['title'][:100]}")
    except Exception as ex:
        print(f"[warn] 네이버 공시 실패 {r['code']}: {ex}", file=sys.stderr)
    try:
        for g in json.loads(get(f"https://m.stock.naver.com/api/news/stock/{r['code']}",
                                params={"pageSize": 10, "page": 1})) or []:
            for x in g.get("items") or []:
                ts = datetime.strptime(x["datetime"], "%Y%m%d%H%M").replace(tzinfo=JST)
                if recent(ts, now, 7):
                    body = re.sub(r"\s+", " ", html.unescape(x.get("body") or ""))[:160]
                    out.append(f"{ts.strftime('%m/%d')} [뉴스] {html.unescape(x.get('title', ''))[:100]} — {body}")
    except Exception as ex:
        print(f"[warn] 네이버 뉴스 실패 {r['code']}: {ex}", file=sys.stderr)
    return out[:12] + google_news(r["name"], "hl=ko&gl=KR&ceid=KR:ko")


# ───────────────────────── 미국(finviz) ─────────────────────────
def us_cap(s):
    m = re.match(r"([\d.]+)([BMK])", s or "")
    return float(m.group(1)) * {"B": 1, "M": 1e-3, "K": 1e-6}[m.group(2)] if m else None  # $B


def us_list(today):
    """finviz는 표의 티커를 일부러 비틀어 보여주므로, 페이지 주석의 'TS 티커|가격|거래량' 목록으로 실제 티커를 맞춘다."""
    try:  # 마지막 거래 시각(S&P500, 뉴욕 현지 시각)으로 거래일 확인
        day = json.loads(get("https://api.stock.naver.com/index/.INX/basic"))["localTradedAt"][:10]
    except Exception as ex:
        raise RuntimeError(f"미국 거래일 확인 실패: {ex}")
    if day != today and not FORCE:
        return day, "", None
    rows, seen = [], set()
    for start in range(1, 2000, 20):
        h = get("https://finviz.com/screener.ashx",
                params={"v": "111", "s": "ta_newhigh", "f": "ind_stocksonly", "o": "-marketcap", "r": start})
        ts = re.search(r"<!-- TS\s*(.*?)\s*TE -->", h, re.S)
        real = {}  # (가격, 거래량) → 실제 티커
        for x in (ts.group(1).split() if ts else []):
            p = x.split("|")
            if len(p) == 3:
                real[(num(p[1]), num(p[2]))] = p[0]
        trs = re.findall(r'<tr class="styled-row[^"]*"[^>]*>(.*?)</tr>', h, re.S)
        new = 0
        for tr in trs:
            c = [text_of(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(c) < 11:
                continue
            tk = real.get((num(c[8]), num(c[10])))
            if not tk or tk in seen:
                continue
            seen.add(tk)
            new += 1
            rows.append({"code": tk, "name": c[2], "mkt": "US", "sector": c[3], "profile": f"{c[3]} / {c[4]} / {c[5]}",
                         "cap": us_cap(c[6]), "price": "$" + c[8], "pct": num(c[9]),
                         "val": f"PER {c[7]}배" if num(c[7]) else ""})
        if new < 20:
            break
        time.sleep(1)
    return day, "finviz New High(주식만) 기준", rows


def us_news(r, now):
    out = []
    try:
        h = get(f"https://finviz.com/quote.ashx?t={r['code']}")
        body = h[h.find('id="news-table"'):]
        day = ""
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body[:body.find("</table>")], re.S)[:30]:
            td = text_of((re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S) or [""])[0])
            a = re.search(r'class="tab-link-news"[^>]*>(.*?)</a>', tr, re.S)
            if not a:
                continue
            m = re.match(r"([A-Z][a-z]{2}-\d\d-\d\d)", td)
            if m:
                day = m.group(1)
            if td.startswith("Today"):
                day = now.strftime("%b-%d-%y")
            try:
                ts = datetime.strptime(day, "%b-%d-%y").replace(tzinfo=now.tzinfo)
            except ValueError:
                continue
            if recent(ts, now, 7):
                out.append(f"{ts.strftime('%m/%d')} [뉴스] {text_of(a.group(1))[:110]}")
            if len(out) >= 8:
                break
    except Exception as ex:
        print(f"[warn] finviz 뉴스 실패 {r['code']}: {ex}", file=sys.stderr)
    return out + google_news(f"{r['name']} stock", "hl=en-US&gl=US&ceid=US:en")


# ───────────────────────── 시장 설정 ─────────────────────────
MARKETS = {
    "JP": {"name": "일본", "label": "도쿄", "tz": "JST", "after": 16, "list": jp_list, "profile": jp_profile, "news": jp_news,
           "unit": "엔", "tick": lambda r: f"{r['code']} JP",
           "naming": "일본 고유명사는 일본어 발음대로 한글 표기(예: 第一興商=다이이치코쇼, 한자의 한국식 독음 금지). "
                     "국내에서 통용되는 한국어 이름이 있으면 그것(예: トヨタ自動車=도요타자동차).",
           "need_ko": True, "link": lambda r: f"https://kabutan.jp/stock/news?code={r['code']}", "link_name": "카부탄 뉴스·공시"},
    "KR": {"name": "한국", "label": "코스피·코스닥", "tz": "JST", "after": 16, "list": kr_list, "profile": kr_profile, "news": kr_news,
           "unit": "원", "tick": lambda r: f"{r['code']} {r['mkt']}", "naming": "", "need_ko": False,
           "link": lambda r: f"https://m.stock.naver.com/domestic/stock/{r['code']}/news", "link_name": "네이버 뉴스·공시"},
    "US": {"name": "미국", "label": "뉴욕", "tz": "ET", "after": 16, "list": us_list, "profile": None, "news": us_news,
           "unit": "$", "tick": lambda r: f"{r['code']} US",
           "naming": "미국 기업은 국내에서 통용되는 한국어 이름(예: Exxon Mobil=엑슨모빌, Coca-Cola=코카콜라). 없으면 영문 그대로.",
           "need_ko": True, "link": lambda r: f"https://finviz.com/quote.ashx?t={r['code']}", "link_name": "finviz 뉴스"},
}


def cap_str(mk, cap):
    if cap is None:
        return "시총 -"
    if mk == "JP":
        return f"시총 {cap / 10000:.1f}조엔" if cap >= 10000 else f"시총 {cap:,.0f}억엔" if cap >= 100 else f"시총 {cap:.1f}억엔"
    if mk == "KR":
        return f"시총 {cap / 10000:.1f}조원" if cap >= 10000 else f"시총 {cap:,.0f}억원"
    return f"시총 ${cap:,.1f}B" if cap >= 1 else f"시총 ${cap * 1000:,.0f}M"


# ───────────────────────── LLM ─────────────────────────
ROLE = "너는 한국 자산운용사 주식운용본부의 해외·국내 주식 담당 애널리스트를 돕는 데스크다. 독자는 일본어·영어를 읽지 않는다."
RULES = ("일본어·영어 문장을 쓰지 마라. 모든 설명은 한국어로 쓴다(영문 약어·제품명 정도만 허용). "
         "JSON 배열만 출력하라. JSON 외 텍스트 금지.")

ITEM_PROMPT = """{role}
오늘({day}) {country} 증시에서 52주 신고가를 경신한 종목들이다. 종목마다 JSON 배열 항목 하나를 출력하라(입력 순서대로, 빠짐없이).

 code: 종목코드 그대로
 ko: 한국어 사명. {naming}
 biz: 무슨 회사인지 한국어 40자 이내. profile(회사 개요·업종)에 근거해 주력 제품·서비스를 구체적으로(예: 반도체 검사장비 제조, 편의점 체인 운영).
 reason: 왜 신고가가 됐는지를 '투자 아이디어'로 한국어 2문장(130자 이내), '~함' 보고서체.
         ① 주가를 끌어올린 사업상 동인(무엇이 잘 되고 있는지)을 먼저 쓴다. 예: 해외(북미·중국 등)에서 제품 판매 호조, 가격 인상 효과,
            신규 수주·수요 확대(AI 데이터센터, 방산, 조선, 인바운드 등), 실적·가이던스 상향, 원가 하락, 정책 수혜, 주주환원 확대.
         ② 그 근거가 된 뉴스·공시를 날짜와 함께 덧붙인다(예: 10/14 상반기 영업이익 예상 상향 공시).
         '증권사 리포트 발간', '목표주가 상향', '신고가 경신', '외국인 순매수' 같은 결과·이벤트만 쓰지 마라. 리포트·목표가 뉴스뿐이면
         그 리포트가 주목한 사업 동인을 쓴다. 뉴스로 동인이 확인되지 않으면 회사 사업 구조와 업종 흐름으로 가장 그럴듯한 동인을 쓰되
         문장 앞에 '(추정)'을 붙여라. 자료에 없는 숫자를 만들지 마라.
 basis: reason의 근거. '공시', '뉴스', '추정' 중 하나.
{rules}

종목:
{items}
"""
ITEM_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "code": {"type": "STRING"}, "ko": {"type": "STRING"}, "biz": {"type": "STRING"},
    "reason": {"type": "STRING"}, "basis": {"type": "STRING"}},
    "required": ["code", "ko", "biz", "reason", "basis"]}}

OTHER_PROMPT = """{role}
오늘({day}) {country} 증시에서 52주 신고가를 경신한 종목들이다. 종목마다 JSON 배열 항목 하나를 출력하라(입력 순서대로, 빠짐없이).

 code: 종목코드 그대로
 ko: 한국어 사명. {naming}
 biz: 무슨 회사인지 한국어 20자 이내(예: 반도체 검사장비, 지방은행, 편의점 체인). 확실하지 않으면 업종 이름.
{rules}

종목:
{items}
"""
OTHER_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "code": {"type": "STRING"}, "ko": {"type": "STRING"}, "biz": {"type": "STRING"}},
    "required": ["code", "ko", "biz"]}}

FLOW_PROMPT = """{role}
오늘({day}) {country} 증시 52주 신고가 종목의 업종 집계와 상위 종목별 신고가 이유다.
'어떤 투자 테마·사업 동인이 신고가를 이끌었는지' 2~3줄로 정리해 JSON 문자열 배열로 출력하라(예: 해외 수요, 업황 회복, 정책).
각 줄 한국어 60자 이내 1문장, '~함' 보고서체. 자료에 있는 사실만 쓰고, 해석이 들어가면 문장 앞에 '(추정)'을 붙여라.
{rules}

[업종 집계(전체 {n}종목)]
{sectors}

[오늘 장 분위기(마감 정리 요약)]
{mood}

[상위 종목]
{tops}
"""
FLOW_SCHEMA = {"type": "ARRAY", "items": {"type": "STRING"}}
NAMING_KR = "원래 한국어 사명 그대로."
BATCH = 10            # 상세 종목은 10개씩 나눠 요청(응답이 잘리거나 빠지는 것 방지)
OTHER_BATCH = 60


def _ask(state, prompt, schema, tag):
    try:
        res = bot.gemini(state, prompt, schema, bot.WRITE_MODELS + bot.TRIAGE_MODELS, tag)
        return res if isinstance(res, list) else []
    except Exception as ex:
        print(f"[warn] {tag} 실패: {ex}", file=sys.stderr)
        return []


def _korean_ok(x):
    """일본어(가나)·영문 문장이 남아 있으면 False."""
    t = " ".join(str(x.get(k) or "") for k in ("biz", "reason"))
    return bool(t.strip()) and not re.search(r"[぀-ヿ]", t) and not re.search(r"[A-Za-z]{4,}(?:\s+[A-Za-z]{3,}){3,}", t)


def describe(state, mk, day, rows, prompt, schema, size, fields):
    """rows를 size개씩 나눠 요청하고, 빠지거나 한국어가 아닌 항목은 한 번 더 요청. 반환: {code: 항목}"""
    cfg = MARKETS[mk]
    out = {}
    for attempt in range(2):
        todo = [r for r in rows if r["code"] not in out]
        for i in range(0, len(todo), size):
            part = todo[i:i + size]
            items = "\n".join(json.dumps({k: r.get(k) for k in fields}, ensure_ascii=False) for r in part)
            res = _ask(state, prompt.format(role=ROLE, rules=RULES, day=day, country=cfg["name"],
                                            naming=cfg["naming"] or NAMING_KR, items=items),
                       schema, f"52주 신고가 {mk} {i // size + 1}/{(len(todo) + size - 1) // size}")
            for x in res:
                if isinstance(x, dict) and str(x.get("code")) in {r["code"] for r in part} and x.get("ko") \
                        and (schema is not ITEM_SCHEMA or _korean_ok(x)):
                    out[str(x["code"])] = x
        if all(r["code"] in out for r in rows):
            break
    return out


DESC_CACHE = {}  # 이번 실행에서 이미 만든 종목 설명(상승률 상위에서 재사용)


def build(state, mk, day, asof, stocks, final):
    """메시지 (머리말, 본문 블록 목록) 반환. 요약이 거의 실패했고 마지막 시도가 아니면 None(다음 실행에 재시도)."""
    cfg = MARKETS[mk]
    now = now_in(cfg["tz"])
    if mk == "JP":  # 일본은 목록에 시총이 없어 모든 종목 페이지를 연다(업종도 여기서)
        for r in stocks[:400]:
            try:
                jp_profile(r)
            except Exception as ex:
                print(f"[warn] 카부탄 종목 페이지 실패 {r['code']}: {ex}", file=sys.stderr)
            time.sleep(0.5)
        n0 = len(stocks)
        stocks[:] = [r for r in stocks if (r.get("cap") or 0) >= HIGH_MIN_CAP]  # 시총 300억 엔 미만 제외
        asof += f", 시총 {HIGH_MIN_CAP:,.0f}억 엔 미만 {n0 - len(stocks)}종목 제외"
    stocks.sort(key=lambda r: -(r.get("cap") or 0))
    top, rest = stocks[:TOP_N], stocks[TOP_N:]
    for r in top:
        if mk == "KR":
            kr_profile(r)
        r["news"] = cfg["news"](r, now)
        time.sleep(0.5)
    sec = Counter(r.get("sector") or "기타" for r in (top if mk == "KR" else stocks))  # 한국은 상위 종목만 업종을 알 수 있음
    by = describe(state, mk, day, top, ITEM_PROMPT, ITEM_SCHEMA, BATCH, ("code", "name", "profile", "pct", "news"))
    DESC_CACHE.update(by)
    if len(by) < max(1, len(top) // 2) and not final:
        print(f"[warn] {mk} 요약 {len(by)}/{len(top)}건만 성공 → 다음 실행에 재시도", file=sys.stderr)
        return None
    shown = rest[:OTHERS_MAX]
    ko_rest = describe(state, mk, day, shown, OTHER_PROMPT, OTHER_SCHEMA, OTHER_BATCH,
                       ("code", "name", "sector", "profile")) if shown else {}
    tops = "\n".join(f"- {(by.get(r['code']) or {}).get('ko') or r['name']}({r.get('sector', '')}): "
                     f"{(by.get(r['code']) or {}).get('reason', '')}" for r in top)
    flow = _ask(state, FLOW_PROMPT.format(role=ROLE, rules=RULES, day=day, country=cfg["name"], n=len(stocks),
                                          sectors="\n".join(f"- {k}: {v}" for k, v in sec.most_common()), tops=tops,
                                          mood=state.get("wrap_ctx") or "(없음)"),
                FLOW_SCHEMA, f"52주 신고가 {mk} 흐름") if by else []
    flow = [str(x).strip() for x in flow if isinstance(x, str) and str(x).strip()][:3]

    head = [f"<b>&gt;&gt;[52주 신고가] {cfg['name']} {day[5:]} {cfg['label']} 마감 · {len(stocks)}종목</b>",
            f"({esc(asof)})"]
    if len(by) < len(top):
        head.append(f"(요약 {len(top) - len(by)}종목 실패: 해당 종목은 원래 사명만 표시)")
    head.append(f"\n<b>[업종{'(상위 ' + str(len(top)) + '종목)' if mk == 'KR' else ''}]</b> "
                + esc(" · ".join(f"{k} {v}" for k, v in sec.most_common(8))))
    if flow:
        head.append("\n<b>[흐름]</b>\n" + "\n\n".join(f"• {esc(x.lstrip('•· '))}" for x in flow))
    blocks = []
    for i, r in enumerate(top, 1):
        w = by.get(r["code"], {})
        name = w.get("ko") or r["name"]
        pct = f"{r['pct']:+.2f}%" if r["pct"] is not None else "-"
        lines = [f"<b>{i}. {esc(name)}({cfg['tick'](r)})</b> {cap_str(mk, r.get('cap'))}",
                 f"종가 {esc(r['price'])}({pct})" + (f" · {esc(r['val'])}" if r.get("val") else "")]
        if w.get("biz"):
            lines.append(f"\n• 사업: {esc(w['biz'])}")
        if w.get("reason"):
            lines.append(f"• 이유: {esc(w['reason'])}")
        lines.append(f"\n&gt;<a href=\"{cfg['link'](r)}\">{cfg['link_name']}</a>")
        blocks.append("\n".join(lines))
    if rest:
        lines = [f"<b>[그 밖의 신고가 {len(rest)}종목]</b> (시총 순, 이름·사업만)"]
        for r in shown:
            o = ko_rest.get(r["code"], {})
            biz = f" — {esc(o['biz'])}" if o.get("biz") else ""
            lines.append(f"• {esc(o.get('ko') or r['name'])}({cfg['tick'](r)}){biz}")
        if len(rest) > len(shown):
            lines.append(f"외 {len(rest) - len(shown)}종목")
        blocks.append("\n".join(lines))
    blocks.append("<i>신고가 이유는 뉴스·공시 제목 기준 요약이며, '(추정)'은 근거 제목이 없는 경우입니다. "
                  "시총·PER·PBR은 각 사이트 표시값입니다.</i>")
    return "\n".join(head), blocks


# ───────────────────────── 일본 증시 마감 정리 ─────────────────────────
WRAP_PICK = [  # 카부탄 시황 기사 중 마감 정리에 쓸 것(제목 패턴, 최대 글자 수)
    (re.compile(r"東京株式（大引け）"), 2200), (re.compile(r"相場概況"), 2200),
    (re.compile(r"寄与度ランキング（大引け）"), 1200), (re.compile(r"東証業種別ランキング"), 600),
    (re.compile(r"東証グロース（大引け）|グロース市況"), 900), (re.compile(r"東証スタンダード（大引け）"), 700),
    (re.compile(r"本日の注目個別銘柄"), 1500), (re.compile(r"話題株ピックアップ【夕刊】"), 1500),
    (re.compile(r"日経平均 大引け｜"), 900),
]


def _article(url, limit):
    h = get("https://s.kabutan.jp" + url)
    i = h.find("news-body")
    t = text_of(re.sub(r"<(script|style).*?</\1>", " ", h[i:i + 30000], flags=re.S)) if i >= 0 else ""
    t = re.sub(r"^news-body[^>]*>\s*", "", t)
    for cut in ("関連記事", "提供：", "人気ニュース"):
        if cut in t:
            t = t[:t.index(cut)]
    return t.strip()[:limit]


def wrap_sources(today):
    """오늘 15시 이후 카부탄 시황 기사 본문 목록 [(제목, 본문)]."""
    seen, picked = set(), {}
    for cat in (1, 2):
        for page in range(1, 9):
            h = get("https://s.kabutan.jp/market_news/", params={"category_org_id": cat, "page": page})
            items = re.findall(r'href="(/news/n\d+/)">\s*([^<]+?)\s*</a>.*?datetime="([^"]+)"', h, re.S)
            if not items:
                break
            for u, t, d in items:
                if u in seen or not d.startswith(today) or d[11:13] < "15":
                    continue
                seen.add(u)
                for k, (pat, lim) in enumerate(WRAP_PICK):
                    if k not in picked and pat.search(t):
                        picked[k] = (html.unescape(t), u, lim)
            if all(d < today for _, _, d in items):
                break
            time.sleep(0.4)
    out = []
    for k in sorted(picked):
        t, u, lim = picked[k]
        try:
            out.append((t, _article(u, lim)))
        except Exception as ex:
            print(f"[warn] 시황 기사 실패 {u}: {ex}", file=sys.stderr)
        time.sleep(0.4)
    return out


def jp_sectors():
    """33업종 등락률 [(업종, %)], 상승률 순."""
    h = get("https://s.kabutan.jp/warnings/sector_stocks_ranking/")
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", h, re.S):
        t = text_of(tr)
        m = re.match(r"(\S+)\s+\d{4}\s+\d+\s+[\d,.]+\s+[+-]?[\d,.]+\s+([+-]?[\d.]+)\s*%", t)
        if m:
            rows.append((SECTOR_KO.get(m.group(1), SECTOR_ALIAS.get(m.group(1), m.group(1))), float(m.group(2))))
    return rows


SECTOR_ALIAS = {"倉庫・運輸": "창고·운수", "証券・商品": "증권", "電気・ガス": "전기·가스", "石油・石炭": "석유·석탄",
                "ガラス・土石": "유리·토석", "繊維": "섬유", "化学工業": "화학"}


def jp_index(sym):
    try:
        d = json.loads(get(f"https://api.stock.naver.com/index/{sym}/basic"))
        r = float(d["fluctuationsRatio"])
        return f"{d['closePrice']} ({r:+.2f}%)" if r else d["closePrice"]  # 다음 날 개장 전에는 등락률이 0으로 초기화됨
    except Exception:
        return ""


WRAP_PROMPT = """{role}
아래는 오늘({day}) 도쿄 증시 마감 후 나온 시황 기사(일본어)와 33업종 등락률이다. 한국 운용역이 읽을 '일본 증시 마감 정리'를 만들어라.
JSON 배열 하나를 출력하되, 항목은 아래 6개 섹션 순서대로 {{"title": 섹션명, "lines": [문장들]}} 형식이다.

1. "한줄 요약": 1문장(70자 이내). 오늘 장의 성격(예: 미 금리·반도체 쇼크발 리스크오프, 내수 방어주로 순환).
2. "지수·수급": 2~3문장. 닛케이·TOPIX·그로스 지수 등락, 상승·하락 종목 수, 매매대금 등 기사에 나온 수치.
3. "오른 업종": 상위 3~5개 업종마다 1문장 '업종(+x.x%): 왜 올랐는지'. 이유는 기사 근거(대표 종목 포함). 근거가 없으면 '(추정)'.
4. "내린 업종": 하위 3~5개 업종마다 같은 형식.
5. "주요 종목": 4~6문장. 지수 영향이 컸거나 화제가 된 종목과 그 이유(실적, 수주, 해외 수요, 업황 등 사업 동인 중심).
   종목명은 한국어(일본어 발음 한글 표기 또는 국내 통용명)로 쓰고 괄호에 종목코드(예: 어드반테스트(6857)).
6. "분위기·체크포인트": 2~3문장. 투자 심리(공포·안도·순환매 등)와 다음 거래일에 볼 변수(미국 지표, 실적 발표, 환율 등). 해석은 '(추정)'.
규칙: 기사에 있는 사실과 수치만 쓰고 숫자를 만들지 마라. 각 문장 '~함' 보고서체. 일본 고유명사는 일본어 발음대로 한글 표기(예: 第一興商=다이이치코쇼).
{rules}

[33업종 등락률(%)]
{sectors}

[지수(네이버 기준)]
닛케이225 {nk} / TOPIX {tp}

[시황 기사]
{articles}
"""
WRAP_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "title": {"type": "STRING"}, "lines": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["title", "lines"]}}


def jp_wrap(state, day, final):
    """일본 증시 마감 정리 메시지. 대표 마감 기사가 아직 없고 마지막 시도가 아니면 None."""
    arts = wrap_sources(day)
    if not any(re.search(r"東京株式（大引け）|相場概況", t) for t, _ in arts) and not final:
        print("[warn] 마감 시황 기사 아직 없음 → 다음 실행에 재시도", file=sys.stderr)
        return None
    try:
        sec = jp_sectors()
    except Exception as ex:
        print(f"[warn] 업종 등락률 실패: {ex}", file=sys.stderr)
        sec = []
    nk, tp = jp_index(".N225"), jp_index(".TOPX")
    res = _ask(state, WRAP_PROMPT.format(role=ROLE, rules=RULES, day=day, nk=nk or "-", tp=tp or "-",
                                         sectors="\n".join(f"- {k}: {v:+.2f}" for k, v in sec) or "(없음)",
                                         articles="\n\n".join(f"## {t}\n{b}" for t, b in arts) or "(없음)"),
               WRAP_SCHEMA, "일본 마감 정리")
    secs = [x for x in res if isinstance(x, dict) and x.get("lines")]
    if len(secs) < 3 and not final:
        print("[warn] 마감 정리 요약 실패 → 다음 실행에 재시도", file=sys.stderr)
        return None
    head = f"<b>&gt;&gt;[일본 증시 마감] {day[5:]}</b>" + (f"\n닛케이225 {esc(nk)} · TOPIX {esc(tp)}" if nk or tp else "")
    blocks = []
    for x in secs:
        blocks.append(f"<b>[{esc(str(x['title']).strip('[] '))}]</b>\n" +
                      "\n\n".join(f"• {esc(str(l).lstrip('•· ').strip())}" for l in x["lines"] if str(l).strip()))
    if sec:
        up = " · ".join(f"{k} {v:+.1f}%" for k, v in sec[:5])
        dn = " · ".join(f"{k} {v:+.1f}%" for k, v in sec[::-1][:5])
        blocks.append(f"<b>[업종 등락 상·하위 5]</b>\n▲ {esc(up)}\n\n▼ {esc(dn)}")
    if not secs:
        blocks.append("(요약 생성 실패: 업종 등락만 보냅니다)")
    blocks.append("<i>카부탄·피스코 마감 시황 기사와 업종 등락률 기준 요약입니다. '(추정)'은 기사에 근거가 없는 해석입니다.</i>")
    state["wrap_ctx"] = " / ".join(" ".join(map(str, x["lines"])) for x in secs[:1] + secs[-1:])[:400]
    return head, blocks


# ───────────────────────── 일본 상승률 상위 ─────────────────────────
GAIN_N = int(os.getenv("GAIN_TOP_N", "30"))
GAIN_MIN_CAP = float(os.getenv("GAIN_MIN_CAP", "300"))  # 억 엔. 상승률 상위에서 이보다 작은 시가총액 종목은 제외
GAIN_POOL = 400  # 시총 필터 전에 모으는 후보 수(300억 엔 이상이 30개 차도록 넉넉히)
GAIN_PROMPT = (ITEM_PROMPT.replace("52주 신고가를 경신한 종목들이다", "상승률 상위 종목들이다")
               .replace("왜 신고가가 됐는지를", "오늘 왜 크게 올랐는지를"))


def jp_gainers(today):
    """카부탄 모바일 '値上がり率' 순위에서 주식만 상위 GAIN_N개. 열: 주가, 등락, 거래량, (프리미엄), PER, PBR, 배당."""
    rows, seen = [], set()
    for page in range(1, 6):
        h = get("https://s.kabutan.jp/warnings/price_increase/", params={"page": page})
        if page == 1:
            m = re.search(r"株価：(\d+)年(\d+)月(\d+)日", h)
            if not m or f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" != today and not FORCE:
                return []
        new = 0
        for tr in re.findall(r"<tr>(.*?)</tr>", h, re.S):
            m = re.search(r'href="/stocks/(\w+)/".*?<p class="font-bold[^"]*">(.*?)</p>.*?(\w+)\s*<span class="px-1[^"]*">(.*?)</span>',
                          tr, re.S)
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if not m or m.group(1) in seen or len(tds) < 7:
                continue
            seen.add(m.group(1))
            new += 1
            mkt = _jp_mkt(m.group(4))
            if mkt in ("東E", "東R", "東I"):
                continue
            pct = re.findall(r"([+-]?[\d.]+)</span><span[^>]*>%", tds[1])
            per, pbr = text_of(tds[4]).replace("倍", "").strip(), text_of(tds[5]).replace("倍", "").strip()
            rows.append({"code": m.group(1), "name": text_of(m.group(2)), "mkt": mkt, "price": text_of(tds[0]) + "엔",
                         "pct": num(pct[0]) if pct else None,
                         "val": " · ".join(x for x in (f"PER {per}배" if num(per) else "", f"PBR {pbr}배" if num(pbr) else "") if x)})
        if not new or len(rows) >= GAIN_POOL:
            break
        time.sleep(0.7)
    rows.sort(key=lambda r: -(r["pct"] or 0))
    return rows  # 시총 필터는 jp_gain_msg에서(종목 페이지에서 시총을 읽음)


HIGH_MIN_CAP = float(os.getenv("HIGH_MIN_CAP", "300"))  # 억 엔. 52주 신고가(일본)에서 이보다 작은 시가총액 종목은 제외

def jp_gain_msg(state, day, rows, highs):
    """상승률 상위 메시지. highs: 오늘 52주 신고가 종목코드 집합(★ 표시)."""
    now = now_in("JST")
    keep = []
    for r in rows:  # 상승률 순서대로 시총을 확인해 GAIN_MIN_CAP 이상만 GAIN_N개
        if len(keep) >= GAIN_N:
            break
        try:
            jp_profile(r)
        except Exception as ex:
            print(f"[warn] 카부탄 종목 페이지 실패 {r['code']}: {ex}", file=sys.stderr)
        time.sleep(0.4)
        if r.get("cap") is not None and r["cap"] >= GAIN_MIN_CAP:
            keep.append(r)
    rows = keep
    todo = [r for r in rows if r["code"] not in DESC_CACHE]
    for r in todo:
        r["news"] = jp_news(r, now)
        time.sleep(0.5)
    by = dict(DESC_CACHE)
    by.update(describe(state, "JP", day, todo, GAIN_PROMPT, ITEM_SCHEMA, BATCH, ("code", "name", "profile", "pct", "news")))
    nh = sum(r["code"] in highs for r in rows)
    head = (f"<b>&gt;&gt;[상승률 상위 {len(rows)}] 일본 {day[5:]} 도쿄 마감</b>\n"
            f"(ETF·리츠·시총 {GAIN_MIN_CAP:,.0f}억 엔 미만 제외, ★ = 오늘 52주 신고가 {nh}종목)")
    blocks = []
    for i, r in enumerate(rows, 1):
        w = by.get(r["code"], {})
        star = "★ " if r["code"] in highs else ""
        pct = f"{r['pct']:+.2f}%" if r["pct"] is not None else "-"
        lines = [f"<b>{i}. {star}{esc(w.get('ko') or r['name'])}({r['code']} JP)</b> {pct}",
                 f"종가 {esc(r['price'])} · {cap_str('JP', r.get('cap'))}" + (f" · {esc(r['val'])}" if r.get("val") else "")]
        if w.get("biz"):
            lines.append(f"\n• 사업: {esc(w['biz'])}")
        if w.get("reason"):
            lines.append(f"• 이유: {esc(w['reason'])}")
        lines.append(f"\n&gt;<a href=\"https://kabutan.jp/stock/news?code={r['code']}\">카부탄 뉴스·공시</a>")
        blocks.append("\n".join(lines))
    blocks.append("<i>상승 이유는 뉴스·공시 제목 기준 요약이며, '(추정)'은 근거 제목이 없는 경우입니다.</i>")
    return head, blocks


TO_PDF = os.getenv("HIGHS_TO_PDF", "1") == "1"  # 신고가·상승률 상위는 텔레그램 대신 다음 날 08:30 아침 PDF(morning.py)에 넣음
def stash(state, kind, day, header, blocks):
    """아침 PDF용으로 메시지(텔레그램 HTML)를 상태에 쌓아 둠"""
    state.setdefault("pdfq", []).append({"ts": time.time(), "kind": kind, "day": day, "head": header, "blocks": blocks})
    print(f"[info] 아침 PDF용 저장: {kind} {day} ({len(blocks)}블록)")
    return True


def send_chunks(header, blocks, sep="\n\n━━━━━━━━━━\n"):
    chunks, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + len(sep) > 3800:
            chunks.append(cur)
            cur = ""
        cur += (sep if cur else "") + b
    chunks.append(cur)
    return all(bot.tg_send(x) for x in chunks)


def run_market(state, mk):
    cfg = MARKETS[mk]
    now = now_in(cfg["tz"])
    today = now.strftime("%Y-%m-%d")
    done = state["last"].get(mk) == today and (mk != "JP" or state["last"].get("JPGAIN") == today)
    if not FORCE and (done or now.hour < cfg["after"] or now.weekday() >= 5):
        print(f"[info] {mk}: 발송 대상 아님(마지막 {state['last'].get(mk)}, 현지 {now:%m-%d %H:%M})")
        return
    try:
        day, asof, rows = cfg["list"](today)
        if rows is not None and mk == "JP" and (state["last"].get("JPWRAP") != today or FORCE):
            tries = state.setdefault("tries", {})
            n = tries["JPWRAP"] = (tries.get("JPWRAP", 0) + 1) if tries.get("JPWRAP_day") == today else 1
            tries["JPWRAP_day"] = today
            w = jp_wrap(state, day, final=FORCE or n >= 2)
            if w is None:
                return  # 마감 정리부터 다시(신고가도 다음 실행에 함께)
            # 마감 정리는 텔레그램으로 보내지 않고 다음 날 08:30 아침 PDF(morning.py '전 거래일 마감 정리')에만 넣음(HIGHS_TO_PDF=0 이면 예전처럼 텔레그램도)
            if stash(state, "wrap", day, *w) if TO_PDF else (send_chunks(*w) and stash(state, "wrap", day, *w)):
                state["last"]["JPWRAP"] = today
    except Exception as ex:
        state["fail"][mk] = state["fail"].get(mk, 0) + 1
        print(f"[warn] {mk} 목록 실패({state['fail'][mk]}회): {ex}", file=sys.stderr)
        if state["fail"][mk] == 2:
            bot.admin_send(f"<b>[봇 경고]</b> 52주 신고가({cfg['name']}) 목록 수집 실패: {esc(str(ex)[:200])}")
        return
    state["fail"][mk] = 0
    if rows is None:  # 휴장일: 데이터가 직전 거래일 그대로
        print(f"[info] {mk}: 데이터 기준일 {day} ≠ 오늘 {today} → 휴장일로 보고 건너뜀")
        state["last"][mk] = today
        return
    n0 = len(rows)
    rows = [r for r in rows if (r.get("pct") or 0) > 0]  # 장중 신고가를 찍고 하락·보합으로 끝난 종목은 제외(상승 마감만)
    if n0 > len(rows):
        asof += f", 하락·보합 마감 {n0 - len(rows)}종목 제외"
    print(f"[info] {mk} {day}: 신고가 {len(rows)}종목(상승 마감, 전체 {n0})")
    if state["last"].get(mk) == today and not FORCE:
        ok = True  # 신고가는 이미 보냄(상승률 상위만 남은 경우)
    elif not rows:
        none_h = f"<b>&gt;&gt;[52주 신고가] {cfg['name']} {day[5:]} {cfg['label']} 마감</b>"
        ok = stash(state, "highs", day, none_h, ["오늘 52주 신고가를 경신한 종목이 없습니다."]) if TO_PDF and mk == "JP" else \
            bot.tg_send(none_h + "\n오늘 52주 신고가를 경신한 종목이 없습니다.")
    else:
        tries = state.setdefault("tries", {})
        n = tries[mk] = (tries.get(mk, 0) + 1) if tries.get(f"{mk}_day") == today else 1
        tries[f"{mk}_day"] = today
        msg = build(state, mk, day, asof, rows, final=FORCE or n >= 2)  # 첫 시도에 요약이 실패하면 30분 뒤 재시도에서 보냄
        if msg is None:
            return
        ok = stash(state, "highs", day, *msg) if TO_PDF and mk == "JP" else send_chunks(*msg)
    if ok:
        state["last"][mk] = today
    if ok and mk == "JP" and (state["last"].get("JPGAIN") != today or FORCE):
        try:
            g = jp_gainers(today)
            if g and (stash(state, "gainers", day, *jp_gain_msg(state, day, g, {r["code"] for r in rows})) if TO_PDF
                      else send_chunks(*jp_gain_msg(state, day, g, {r["code"] for r in rows}))):
                state["last"]["JPGAIN"] = today
            elif not g:
                state["last"]["JPGAIN"] = today
        except Exception as ex:
            print(f"[warn] 상승률 상위 실패: {ex}", file=sys.stderr)


def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else "auto").upper()
    if arg == "AUTO":
        h = datetime.now(JST).hour
        mks = ["JP"] if 16 <= h <= 23 else []  # 한국·미국 신고가는 보내지 않음(코드만 유지, 수동 실행은 가능)
    else:
        mks = [m for m in arg.split(",") if m in MARKETS]
    state = load_state()
    for mk in mks:
        run_market(state, mk)
        save_state(state)


if __name__ == "__main__":
    main()
