#!/usr/bin/env bash
# 봇 상태 파일은 main이 아니라 'state' 브랜치에 저장(코드 이력과 분리 · 하루 수백 건의 상태 커밋이 main을 덮지 않게)
#   bash state.sh pull            → 상태 파일을 작업 폴더로 가져옴(최신 것)
#   bash state.sh push 파일...     → 그 파일들을 state 브랜치에 저장(다른 봇과 겹치면 재시도)
# 옮기는 기간 안전장치: main과 state 브랜치 중 더 최근에 저장된 쪽을 씀(예전 코드로 돌던 실행이 main에 남긴 것도 놓치지 않음)
set -u
FILES="state.json highs_state.json community_state.json morning_state.json weather_state.json"
BR=state

pull() {
  git fetch -q --depth=100 origin +refs/heads/main:refs/remotes/origin/main "+refs/heads/$BR:refs/remotes/origin/$BR" 2>/dev/null \
    || git fetch -q --depth=100 origin +refs/heads/main:refs/remotes/origin/main || true
  for f in $FILES; do
    [ -f ".own_$f" ] && continue   # 이 실행이 직접 쓰는 파일은 덮어쓰지 않음(저장이 한 번 실패해도 내용 유지)
    ts=$(git log -1 --format=%ct "origin/$BR" -- "$f" 2>/dev/null || echo 0)
    tm=$(git log -1 --format=%ct origin/main -- "$f" 2>/dev/null || echo 0)
    if [ "${ts:-0}" -ge "${tm:-0}" ] && [ "${ts:-0}" != "0" ]; then src="origin/$BR"; else src=origin/main; fi
    git show "$src:$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f" || rm -f "$f.tmp"
  done
  echo "[state] 상태 파일 가져옴(state 브랜치 우선, 더 최근이면 main)"
}

push() {
  [ $# -gt 0 ] || return 0
  local W msg
  W=$(mktemp -d)
  msg="${STATE_MSG:-state update} [skip ci]"
  for f in "$@"; do touch ".own_$f"; done
  for i in 1 2 3 4 5; do
    rm -rf "$W"
    git fetch -q --depth=20 origin "+refs/heads/$BR:refs/remotes/origin/$BR" 2>/dev/null
    if git rev-parse -q --verify "origin/$BR" >/dev/null; then
      git worktree add -q -f --detach "$W" "origin/$BR" || { sleep $((i * 3)); continue; }
    else  # 처음 한 번: state 브랜치 새로 만들기
      git worktree add -q -f --detach "$W" HEAD || return 1
    fi
    for f in "$@"; do [ -f "$f" ] && cp "$f" "$W/$f"; done
    (
      cd "$W" || exit 1
      git add -- "$@" 2>/dev/null
      git diff --cached --quiet && exit 0
      git -c user.name="${GIT_NAME:-jpnews-bot}" -c user.email="actions@users.noreply.github.com" commit -qm "$msg" || exit 1
      git push -q origin "HEAD:refs/heads/$BR"
    )
    rc=$?
    git worktree remove --force "$W" 2>/dev/null; rm -rf "$W"
    [ $rc -eq 0 ] && return 0
    sleep $((i * 5))
  done
  echo "::warning::상태 저장 실패($*)"
  return 1
}

cmd=${1:-}; shift || true
case "$cmd" in
  pull) pull ;;
  push) push "$@" ;;
  *) echo "사용: bash state.sh pull | push 파일..."; exit 2 ;;
esac
