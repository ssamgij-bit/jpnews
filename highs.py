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


def google_news(q, params, days=3):
    try:
        f = feedparser.parse(requests.get(
            f"https://news.google.com/rss/search?{params}&q={urllib.parse.quote(f'{q} when:{days}d')}",
            headers=UA, timeout=20).content)
        return ["[구글뉴스] " + bot.SUFFIX_RE.sub("", html.unescape(e.title)).strip()[:90] for e in f.entries[:3]]
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


def jp_list(today):
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
    m2 = re.match(r"(?:(\d+)兆)?(\d+)?億円", text_of(m.group(1)).replace(",", "").replace(" ", "")) if m else None
    return int(m2.group(1) or 0) * 10000 + int(m2.group(2) or 0) if m2 else None


def jp_profile(r):
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
                    out.append(f"{ts.strftime('%m/%d')} [뉴스] {html.unescape(x.get('title', ''))[:100]}")
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
        return f"시총 {cap / 10000:.1f}조엔" if cap >= 10000 else f"시총 {cap:,.0f}억엔"
    if mk == "KR":
        return f"시총 {cap / 10000:.1f}조원" if cap >= 10000 else f"시총 {cap:,.0f}억원"
    return f"시총 ${cap:,.1f}B" if cap >= 1 else f"시총 ${cap * 1000:,.0f}M"


# ───────────────────────── LLM ─────────────────────────
PROMPT = """너는 한국 자산운용사 주식운용본부의 해외·국내 주식 담당 애널리스트를 돕는 데스크다.
오늘({day}) {country} 증시에서 52주 신고가를 경신한 종목들이다. JSON 객체 하나만 출력하라.

출력:
 flow: 오늘 신고가 종목 전체(업종 집계와 DETAIL 자료)를 보고 '어떤 흐름이 신고가를 이끌었는지' 2~3줄. 각 줄 60자 이내 1문장, '~함' 보고서체.
       자료에 있는 사실만 쓰고, 해석이 들어가면 문장 앞에 '(추정)'을 붙여라.
 items: DETAIL 종목마다 하나.
   code: 종목코드 그대로
   ko: 한국어 사명. {naming}
   biz: 무슨 회사인지 40자 이내. profile(회사 개요·업종)에 근거해 주력 제품·서비스를 구체적으로.
   reason: 왜 신고가가 됐는지 1~2문장(90자 이내), '~함' 보고서체. news(뉴스·공시 제목)에 근거가 있으면 그 재료를 날짜와 함께
           구체적으로 써라(예: 10/14 상반기 실적 상향 공시). 근거가 될 제목이 없으면 문장 앞에 '(추정)'을 붙이고 업종 흐름 등으로 짐작하되,
           모르면 '(추정) 뚜렷한 개별 재료 확인 안 됨'이라고 써라. 제목에 없는 숫자를 만들지 마라.
   basis: reason의 근거. '공시', '뉴스', '추정' 중 하나.
 others: OTHERS 종목마다 {{"code": 종목코드, "ko": 한국어 사명}} (OTHERS가 없으면 빈 배열).
일본어를 쓰지 마라(영문 사명은 그대로 써도 됨). JSON 외 텍스트 금지.

[업종 집계(전체 {n}종목)]
{sectors}

DETAIL:
{detail}

OTHERS:
{others}
"""
SCHEMA = {"type": "OBJECT", "properties": {
    "flow": {"type": "ARRAY", "items": {"type": "STRING"}},
    "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "code": {"type": "STRING"}, "ko": {"type": "STRING"}, "biz": {"type": "STRING"},
        "reason": {"type": "STRING"}, "basis": {"type": "STRING"}},
        "required": ["code", "ko", "biz", "reason", "basis"]}},
    "others": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "code": {"type": "STRING"}, "ko": {"type": "STRING"}}, "required": ["code", "ko"]}}},
    "required": ["flow", "items", "others"]}


def build(state, mk, day, asof, stocks):
    cfg = MARKETS[mk]
    now = now_in(cfg["tz"])
    if mk == "JP":  # 일본은 목록에 시총이 없어 모든 종목 페이지를 연다(업종도 여기서)
        for r in stocks[:400]:
            try:
                jp_profile(r)
            except Exception as ex:
                print(f"[warn] 카부탄 종목 페이지 실패 {r['code']}: {ex}", file=sys.stderr)
            time.sleep(0.5)
    stocks.sort(key=lambda r: -(r.get("cap") or 0))
    top, rest = stocks[:TOP_N], stocks[TOP_N:]
    for r in top:
        if mk == "KR":
            kr_profile(r)
        r["news"] = cfg["news"](r, now)
        time.sleep(0.5)
    sec = Counter(r.get("sector") or "기타" for r in stocks)
    if mk == "KR":  # 한국은 상위 종목만 업종을 알 수 있음
        sec = Counter(r.get("sector") or "기타" for r in top)
    sectors = "\n".join(f"- {k}: {v}" for k, v in sec.most_common())
    detail = "\n".join(json.dumps({"code": r["code"], "name": r["name"], "profile": r.get("profile", ""),
                                   "pct": r["pct"], "news": r.get("news", [])}, ensure_ascii=False) for r in top)
    others = "\n".join(json.dumps({"code": r["code"], "name": r["name"]}, ensure_ascii=False)
                       for r in rest[:OTHERS_MAX]) if cfg["need_ko"] and mk != "US" else ""
    try:
        res = bot.gemini(state, PROMPT.format(day=day, country=cfg["name"], naming=cfg["naming"] or "원래 사명 그대로.",
                                              n=len(stocks), sectors=sectors, detail=detail, others=others or "(없음)"),
                         SCHEMA, bot.WRITE_MODELS + bot.TRIAGE_MODELS, f"52주 신고가 {mk}")
        ok = isinstance(res, dict)
    except Exception as ex:
        print(f"[warn] 신고가 정리 실패 {mk}: {ex}", file=sys.stderr)
        res, ok = {}, False
    by = {str(x.get("code")): x for x in (res.get("items") or []) if isinstance(x, dict)}
    ko_rest = {str(x.get("code")): x.get("ko") for x in (res.get("others") or []) if isinstance(x, dict)}

    head = [f"<b>&gt;&gt;[52주 신고가] {cfg['name']} {day[5:]} {cfg['label']} 마감 · {len(stocks)}종목</b>",
            f"({esc(asof)})"]
    if not ok:
        head.append("(요약 생성 실패: 원래 사명과 수치만 보냅니다)")
    head.append(f"\n<b>[업종{'(상위 ' + str(len(top)) + '종목)' if mk == 'KR' else ''}]</b> "
                + esc(" · ".join(f"{k} {v}" for k, v in sec.most_common(8))))
    flow = [str(x).strip() for x in (res.get("flow") or []) if str(x).strip()][:3]
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
        shown = rest[:OTHERS_MAX]
        names = ", ".join(f"{esc(ko_rest.get(r['code']) or r['name'])}({r['code']})" if mk != "US" else r["code"]
                          for r in shown)
        more = f" 외 {len(rest) - len(shown)}종목" if len(rest) > len(shown) else ""
        blocks.append(f"<b>[그 밖의 신고가 {len(rest)}종목]</b> (시총 순)\n{names}{more}")
    blocks.append("<i>신고가 이유는 뉴스·공시 제목 기준 요약이며, '(추정)'은 근거 제목이 없는 경우입니다. "
                  "시총·PER·PBR은 각 사이트 표시값입니다.</i>")
    return "\n".join(head), blocks


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
    if not FORCE and (state["last"].get(mk) == today or now.hour < cfg["after"] or now.weekday() >= 5):
        print(f"[info] {mk}: 발송 대상 아님(마지막 {state['last'].get(mk)}, 현지 {now:%m-%d %H:%M})")
        return
    try:
        day, asof, rows = cfg["list"](today)
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
    print(f"[info] {mk} {day}: 신고가 {len(rows)}종목")
    if not rows:
        ok = bot.tg_send(f"<b>&gt;&gt;[52주 신고가] {cfg['name']} {day[5:]} {cfg['label']} 마감</b>\n"
                         "오늘 52주 신고가를 경신한 종목이 없습니다.")
    else:
        ok = send_chunks(*build(state, mk, day, asof, rows))
    if ok:
        state["last"][mk] = today


def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else "auto").upper()
    if arg == "AUTO":
        h = datetime.now(JST).hour
        mks = ["JP", "KR"] if 16 <= h <= 23 else ["US"] if 5 <= h <= 12 else []
    else:
        mks = [m for m in arg.split(",") if m in MARKETS]
    state = load_state()
    for mk in mks:
        run_market(state, mk)
        save_state(state)


if __name__ == "__main__":
    main()
