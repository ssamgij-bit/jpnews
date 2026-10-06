"""상장사 목록 갱신: JPX(일본 내국·외국 주식)와 KOSPI·KOSDAQ 종목을 data/*.csv로 저장.

티커 대조표용. refresh-tickers 워크플로우가 매월 1회(수동 실행 가능) 돌린다.
"""
import csv
import io
import os
import re
import sys
import time

import openpyxl
import requests

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
BASE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(BASE, "data"), exist_ok=True)


def jpx():
    page = requests.get("https://www.jpx.co.jp/markets/statistics-equities/misc/01.html", headers=UA, timeout=30).text
    m = re.search(r'href="([^"]+data_j\.xlsx?)"', page)
    url = "https://www.jpx.co.jp" + m.group(1)
    wb = openpyxl.load_workbook(io.BytesIO(requests.get(url, headers=UA, timeout=60).content), read_only=True)
    rows = []
    for r in list(wb.active.iter_rows(values_only=True))[1:]:
        if r[3] and ("内国株式" in r[3] or "外国株式" in r[3]):
            rows.append((str(r[1]), str(r[2]).strip()))
    return rows


def krx():
    rows = []
    for mkt, suf in (("KOSPI", "KS"), ("KOSDAQ", "KQ")):
        p = 1
        while p < 60:
            d = requests.get(f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}?page={p}&pageSize=100",
                             headers=UA, timeout=30).json()
            st = d.get("stocks") or []
            if not st:
                break
            rows += [(s["itemCode"], s["stockName"], suf) for s in st if s.get("stockEndType") == "stock"]
            p += 1
            time.sleep(0.3)
    return rows


def write(name, header, rows, minimum):
    if len(rows) < minimum:  # 비정상적으로 적으면 기존 파일 유지
        print(f"[warn] {name}: {len(rows)}건뿐이라 갱신하지 않음", file=sys.stderr)
        return
    with open(os.path.join(BASE, "data", name), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"[info] {name}: {len(rows)}건")


if __name__ == "__main__":
    for fn, name, header, minimum in ((jpx, "jpx_list.csv", ["code", "name"], 3000),
                                      (krx, "krx_list.csv", ["code", "name", "suffix"], 2000)):
        try:
            write(name, header, fn(), minimum)
        except Exception as ex:
            print(f"[warn] {name} 갱신 실패: {ex}", file=sys.stderr)
