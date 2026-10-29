"""봇 상태 점검(평일 아침 브리핑 직후 1회). 문제가 있을 때만 관리자 대화창으로 알림.

점검: 이어 달리기로 도는 워크플로가 지금도 돌고 있는지(실행 중·대기 중이 없거나 마지막 실행이 오래됐으면 끊김),
최근 실행이 연속으로 실패했는지. 공개 저장소라 GitHub API를 토큰 없이 읽는다(GH_TOKEN이 있으면 사용).
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_CHAT_ID(없으면 TELEGRAM_CHAT_ID). 선택: HEALTH_ALWAYS=1(문제 없어도 보냄), DRY_RUN=1
"""
import os
import sys
import time
from datetime import datetime, timezone

import requests

import bot
from bot import esc

# (저장소, 워크플로 파일, 이름, 끊김으로 볼 시간(시간) — 이어 달리기는 한 실행이 최대 약 5.5시간)
CHECKS = [
    ("ssamgij-bit/jpnews", "weather.yml", "서울 날씨", 7),
    ("ssamgij-bit/jpnews", "community.yml", "커뮤니티·X·note", 7),
    ("ssamgij-bit/jpnews", "highs.yml", "신고가·상승률·마감 정리", 7),
    ("ssamgij-bit/jpnews", "morning.yml", "아침 브리핑 PDF", 7),
    ("ssamgij-bit/jpnews", "run.yml", "일본 뉴스봇", 7),
    ("ssamgij-bit/jpall", "earn.yml", "실적 스크리닝", 2),
    ("ssamgij-bit/jpall", "jpall.yml", "공시 채널(JP_ALL)", 3),
]


def runs(repo, wf):
    h = {"Accept": "application/vnd.github+json"}
    if os.getenv("GH_TOKEN") and repo.endswith("/jpnews"):
        h["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs", params={"per_page": 10}, headers=h, timeout=30)
    r.raise_for_status()
    return r.json().get("workflow_runs", [])


def ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def main():
    probs, oks = [], []
    for repo, wf, name, gap_h in CHECKS:
        try:
            rs = runs(repo, wf)
        except Exception as ex:
            probs.append(f"{name}: 실행 기록을 못 읽음({str(ex)[:60]})")
            continue
        live = [r for r in rs if r["status"] in ("in_progress", "queued", "pending", "waiting")]
        done = [r for r in rs if r["status"] == "completed"]
        last = max((ts(r["updated_at"]) for r in rs), default=0)
        age = (time.time() - last) / 3600 if last else 999
        fails = 0
        for r in done:
            if r["conclusion"] == "failure":
                fails += 1
            else:
                break
        if not live and age > gap_h:
            probs.append(f"{name}: 멈춤 의심 — 실행 중인 회차가 없고 마지막 실행이 {age:.0f}시간 전")
        elif fails >= 2:
            probs.append(f"{name}: 최근 {fails}회 연속 실패")
        else:
            oks.append(name)
    if not probs and os.getenv("HEALTH_ALWAYS") != "1":
        print(f"[info] 이상 없음: {', '.join(oks)}")
        return
    msg = "<b>[봇 상태 점검]</b> " + datetime.now(bot.JST).strftime("%m-%d %H:%M") + "\n"
    msg += "\n".join(f"• {esc(p)}" for p in probs) if probs else "이상 없음"
    if oks:
        msg += "\n\n정상: " + esc(", ".join(oks))
    if probs:
        msg += "\n\n다시 잇기: GitHub Actions에서 해당 워크플로를 '이어 달리기 실행'에 체크하고 Run workflow."
    bot.admin_send(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print(f"[warn] 상태 점검 실패: {ex}", file=sys.stderr)
