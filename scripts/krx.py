"""KRX OPEN API 일별매매정보(유가증권·코스닥) 전종목 스냅샷을 날짜별로 저장한다.

저장: data/px/YYYYMMDD.csv.gz  (code,name,market,close,vol,value,mktcap,shares)
 - KRX OPEN API 는 당일 자료가 다음 날 아침에 올라온다.
 - 휴장일은 빈 응답이므로 파일을 만들지 않고 data/px/_empty.json 에 기록한다.

사용법:  py scripts/krx.py --days 10
         py scripts/krx.py --from 2025-07-01
"""
import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
PX = ROOT / "data" / "px"
EMPTY = PX / "_empty.json"
BASE = "http://data-dbg.krx.co.kr/svc/apis/sto"
KEY = os.environ.get("KRX_API_KEY", "").strip()
S = requests.Session()
COLS = ["code", "name", "market", "close", "vol", "value", "mktcap", "shares"]


def num(s):
    try:
        return int(float(str(s).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def fetch_day(d):
    bd = d.strftime("%Y%m%d")
    rows = []
    for ds, mk in (("stk_bydd_trd", "KOSPI"), ("ksq_bydd_trd", "KOSDAQ")):
        out = None
        for i in range(4):
            try:
                r = S.get(f"{BASE}/{ds}", params={"basDd": bd},
                          headers={"AUTH_KEY": KEY}, timeout=30)
                out = r.json().get("OutBlock_1", []) or []
                break
            except (requests.RequestException, ValueError):
                time.sleep(1.5 * (i + 1))
        if out is None:
            raise RuntimeError(f"KRX 요청 실패 {bd} {ds}")
        for it in out:
            rows.append([it["ISU_CD"], it.get("ISU_NM", ""), mk, num(it.get("TDD_CLSPRC")),
                         num(it.get("ACC_TRDVOL")), num(it.get("ACC_TRDVAL")),
                         num(it.get("MKTCAP")), num(it.get("LIST_SHRS"))])
        time.sleep(0.05)
    return rows


def save_day(d, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLS)
    w.writerows(rows)
    with gzip.open(PX / f"{d:%Y%m%d}.csv.gz", "wb") as f:
        f.write(buf.getvalue().encode("utf-8"))


def load_day(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run(d_from, d_to):
    if not KEY:
        raise SystemExit("환경변수 KRX_API_KEY 가 필요합니다.")
    PX.mkdir(parents=True, exist_ok=True)
    empty = set(json.loads(EMPTY.read_text()) if EMPTY.exists() else [])
    today = dt.date.today()
    got, d = 0, d_from
    while d <= d_to:
        key = f"{d:%Y%m%d}"
        if d.weekday() < 5 and not (PX / f"{key}.csv.gz").exists() and key not in empty:
            rows = fetch_day(d)
            if rows:
                save_day(d, rows)
                got += 1
                if got % 25 == 0:
                    print(f"  {key} ({got}일 저장)", flush=True)
            elif (today - d).days > 3:           # 최근 3일은 아직 미게시일 수 있어 기록하지 않음
                empty.add(key)
        d += dt.timedelta(days=1)
    EMPTY.write_text(json.dumps(sorted(empty)))
    print(f"[KRX] 신규 {got}일 저장, 누적 {len(list(PX.glob('*.csv.gz')))}일", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--from", dest="d_from")
    a = ap.parse_args()
    d_to = dt.date.today()
    d_from = dt.date.fromisoformat(a.d_from) if a.d_from else d_to - dt.timedelta(days=a.days)
    run(d_from, d_to)
