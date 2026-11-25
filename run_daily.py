"""하루 한 번 실행하는 갱신 스크립트 (Windows·macOS 공용).

순서: KIND 신청·체결 → KRX 시세 → DART 프로그램 → 대시보드 JSON 빌드 → (선택) git push

 - KIND 체결·신청내역은 장 마감 후 18~20시대에 올라온다. 20:30 이후 실행을 권장한다.
 - KRX OPEN API 시세는 다음 날 아침에 올라오므로, 저녁 실행분의 주가는 전일 종가 기준이다.
   아침(08:30 전후)에 한 번 더 돌리면 시세까지 당일 기준으로 맞는다.
 - 환경변수 DART_API_KEY, KRX_API_KEY 가 필요하다. 프로젝트 루트의 .env 파일도 읽는다.

사용법:  python run_daily.py            (갱신만)
         python run_daily.py --push     (갱신 후 docs/ 와 data/ 를 커밋·푸시)
         python run_daily.py --backfill (갱신 뒤 과거 13개월치 중 빠진 문서를 200건까지 추가 수집)
"""
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def step(name, *args):
    print(f"\n=== {name} ===", flush=True)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / args[0]), *args[1:]], cwd=ROOT)
    return r.returncode


def main():
    load_env()
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] 자사주 매입 모니터 갱신", flush=True)
    fails = []
    if "--push" in sys.argv:              # 다른 곳에서 올린 갱신분을 먼저 받는다
        subprocess.run(["git", "pull", "-q", "--rebase", "--autostash"], cwd=ROOT)
    if step("KIND 신청·체결", "kind.py", "--days", "10"):
        fails.append("kind")
    elif "--backfill" in sys.argv:        # 과거분을 조금씩 채운다(요청 제한 때문에 한 번에 200건)
        since = (dt.date.today() - dt.timedelta(days=400)).isoformat()
        step("KIND 과거분 보충", "kind.py", "--from", since, "--max-docs", "200")
    if step("KRX 시세", "krx.py", "--days", "10"):
        fails.append("krx")
    if step("DART 프로그램", "dart.py", "--days", "15", "--fill-missing"):
        fails.append("dart")
    if step("빌드", "build.py"):
        fails.append("build")
    if "--push" in sys.argv and "build" not in fails:
        subprocess.run(["git", "add", "docs", "data"], cwd=ROOT)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
            subprocess.run(["git", "commit", "-q", "-m", f"data: {dt.date.today()}"], cwd=ROOT)
            subprocess.run(["git", "push", "-q"], cwd=ROOT)
            print("푸시 완료", flush=True)
        else:
            print("변경 없음", flush=True)
    if fails:
        print("실패 단계:", ", ".join(fails), flush=True)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
