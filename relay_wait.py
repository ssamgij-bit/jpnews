"""이어 달리기용 대기: 다음 실행 시각(JST)까지 기다린다.

GitHub 예약 실행(cron)은 몇 시간씩 밀리거나 빠져서 시각을 맞출 수 없다. 그래서 워크플로가
이 스크립트로 '다음 시각까지' 기다렸다가 봇을 돌리고, 끝날 때 다음 실행을 직접 띄운다(이어 달리기).

사용: python relay_wait.py 시각목록 [요일] [최대대기초]
  시각목록: "16:40,17:10" 또는 매시 "*:50"
  요일: "0-4"(월~금) 처럼. 비우면 매일
  최대대기초: 이만큼 기다려도 시각이 안 오면 종료 코드 3(→ 다음 실행에 넘김)
종료 코드 0 = 시각 도착(봇 실행), 3 = 아직(대기만 하고 끝).
시각이 2분 이내로 지났으면 바로 0(방금 놓친 회차는 실행).
"""
import sys
import time
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
GRACE = 120


def days_of(spec):
    if not spec:
        return set(range(7))
    out = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out |= set(range(int(a), int(b or a) + 1))
    return out


def next_target(times, days, now):
    cands = []
    for d in range(0, 8):
        base = (now + timedelta(days=d)).replace(second=0, microsecond=0)
        if base.weekday() not in days:
            continue
        for t in times.split(","):
            h, m = t.split(":")
            hours = range(24) if h == "*" else [int(h)]
            for hh in hours:
                x = base.replace(hour=hh, minute=int(m))
                if (now - x).total_seconds() <= GRACE:
                    cands.append(x)
        if cands:
            return min(cands)
    return None


def main():
    times = sys.argv[1]
    days = days_of(sys.argv[2] if len(sys.argv) > 2 else "")
    max_wait = float(sys.argv[3]) if len(sys.argv) > 3 else 5 * 3600 + 20 * 60
    now = datetime.now(JST)
    t = next_target(times, days, now)
    wait = (t - now).total_seconds() if t else float("inf")
    if wait > max_wait:
        print(f"[relay] 다음 시각 {t:%m-%d %H:%M} JST까지 {wait / 3600:.1f}시간 → {max_wait / 60:.0f}분 대기 후 다음 실행에 넘김", flush=True)
        time.sleep(max(0, max_wait))
        sys.exit(3)
    if wait > 0:
        print(f"[relay] {t:%m-%d %H:%M} JST까지 {wait / 60:.0f}분 대기", flush=True)
        time.sleep(wait)
    print(f"[relay] 시각 도착({datetime.now(JST):%H:%M:%S} JST) → 실행", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
