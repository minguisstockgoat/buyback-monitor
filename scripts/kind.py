"""KIND 거래소 공시 '자기주식매매 신청내역/체결내역' 수집기.

원본: kind.krx.co.kr 상세검색 > 시장조치 > 자기주식매매신청/체결내역 (공시유형 0326)
 - 체결내역: 당일 체결수량·체결금액. 직접취득은 누적체결량·신고수량 포함.
   (유가증권 문서에만 신청수량이 있고 금액이 천원 단위다. 코스닥은 원 단위이며
    신청수량은 전일 신청내역 문서에서 가져와야 한다. amount 열은 원으로 통일한다.)
 - 신청내역: 다음 거래일에 낼 주문(신청수량·호가시기).
결과는 data/kind_exec.csv, data/kind_orders.csv 에 누적한다.

사용법:  py scripts/kind.py --days 10        (최근 10일 갱신)
         py scripts/kind.py --from 2025-10-01 (백필)
"""
import argparse
import csv
import datetime as dt
import html
import json
import re
import sys
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EXEC_CSV = DATA / "kind_exec.csv"
ORD_CSV = DATA / "kind_orders.csv"
DOCS_JSON = DATA / "kind_docs.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BASE = "https://kind.krx.co.kr"
EXEC_COLS = ["date", "market", "code", "name", "kind", "side", "req", "exec",
             "amount", "cum", "declared", "avg_price", "note", "acptno"]
ORD_COLS = ["date", "market", "code", "name", "kind", "side", "req", "timing",
            "note", "acptno"]

_local = threading.local()


def sess():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": UA,
                          "Referer": BASE + "/disclosure/details.do?method=searchDetailsMain"})
        _local.s = s
    return _local.s


PACE = 1.5                      # 요청 간 최소 간격(초)
_gate = threading.Lock()
_last = [0.0]


class RateLimited(RuntimeError):
    """KIND 가 429 를 주면 IP 가 1시간가량 막힌다(Retry-After: 3600). 재시도하지 않고 멈춘다."""


def get(url, **kw):
    method = kw.pop("method", "GET")
    for i in range(4):
        with _gate:
            wait = PACE - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = sess().request(method, url, timeout=30, **kw)
            if r.status_code == 429:
                raise RateLimited(f"429 (Retry-After {r.headers.get('Retry-After', '?')}초)")
            if r.status_code == 200 and "페이지 오류" not in r.text[:3000]:
                return r
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"KIND 요청 실패: {url}")


def list_docs(d_from, d_to):
    """기간 내 0326 공시 목록 → [(acptno, title)]"""
    out, page = [], 1
    while True:
        r = get(BASE + "/disclosure/details.do", method="POST", data={
            "method": "searchDetailsSub", "currentPageSize": "50", "pageIndex": str(page),
            "orderMode": "1", "orderStat": "D", "forward": "details_sub",
            "disclosureType02": "0326|", "disclosureTypeArr02": "0326|",
            "fromDate": d_from.isoformat(), "toDate": d_to.isoformat(),
            "reportNm": "", "reportCd": "", "searchCodeType": "",
            "repIsuSrtCd": "", "searchCorpName": "",
        })
        found = re.findall(r"openDisclsViewer\('(\d+)',''\)\"\s*title='([^']+)'", r.text)
        out += found
        if len(found) < 50:
            break
        page += 1
    return out


def classify(title):
    t = title.replace(" ", "")
    if "코넥스" in t:
        return None
    market = "KOSPI" if "유가증권" in t else "KOSDAQ" if "코스닥" in t else None
    typ = "exec" if "체결" in t else "order" if "신청" in t else None
    if not market or not typ:
        return None
    return market, typ


def fetch_doc_html(acptno):
    r = get(f"{BASE}/common/disclsviewer.do?method=search&acptno={acptno}&docno=&viewerhost=&viewerport=")
    m = re.search(r"<option[^>]*value=['\"](\d+)\|", r.text)
    if not m:
        raise RuntimeError(f"docNo 없음: {acptno}")
    r = get(f"{BASE}/common/disclsviewer.do?method=searchContents&docNo={m.group(1)}")
    m = re.search(r"setPath\([^,]*,\s*['\"]([^'\"]+)['\"]", r.text)
    if not m:
        raise RuntimeError(f"본문 경로 없음: {acptno}")
    b = get(m.group(1)).content
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp949", "replace")


def table_rows(doc):
    x = re.sub(r"<(script|style).*?</\1>", "", doc, flags=re.S | re.I)
    for tr in re.findall(r"<tr.*?</tr>", x, flags=re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).replace("\xa0", " ")
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)]
        yield [re.sub(r"\s+", " ", c).strip() for c in cells]


def n(s):
    s = (s or "").replace(",", "").strip()
    if s in ("", "-"):
        return ""
    try:
        return str(int(float(s)))
    except ValueError:
        return ""


HEAD = {"구분": "side", "종목": "name", "종목명": "name", "종목코드": "code",
        "신고서구분": "kind", "신청수량": "req", "체결수량": "exec",
        "체결금액(천원)": "amount_k", "체결금액": "amount", "체결금액(원)": "amount",
        "누적체결량": "cum", "신고수량": "declared",
        "평균체결가(원)": "avg_price", "평균체결가": "avg_price",
        "호가시기": "timing", "비고": "note"}
NUM = {"req", "exec", "amount_k", "amount", "cum", "declared", "avg_price"}


def parse_doc(doc, market, acptno):
    date, header, out = None, None, []
    for cells in table_rows(doc):
        if len(cells) == 2 and "일자" in cells[0]:
            m = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", cells[1])
            if m:
                date = "%04d-%02d-%02d" % tuple(map(int, m.groups()))
            continue
        if "종목코드" in cells:
            header = [HEAD.get(c.replace(" ", "")) for c in cells]
            continue
        if not header or len(cells) != len(header):
            continue
        row = {"market": market, "acptno": acptno}
        for k, v in zip(header, cells):
            if not k:
                continue
            row[k] = n(v) if k in NUM else v
        code = re.sub(r"^A", "", row.get("code", ""))
        if not re.fullmatch(r"[0-9A-Z]{6}", code):
            continue
        row["code"] = code
        if row.get("amount_k"):          # 유가증권은 천원 단위, 코스닥은 원 단위
            row["amount"] = str(int(row["amount_k"]) * 1000)
        out.append(row)
    if not date:
        raise RuntimeError(f"일자 없음: {acptno}")
    for r in out:
        r["date"] = date
    return out


def load_csv(p):
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(p, rows, cols):
    rows.sort(key=lambda r: (r["date"], r["market"], r["kind"], r["code"], r.get("side", "")))
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(p)


def run(d_from, d_to, max_docs=0):
    DATA.mkdir(exist_ok=True)
    done = json.loads(DOCS_JSON.read_text(encoding="utf-8")) if DOCS_JSON.exists() else {}
    todo, cur = [], d_from
    while cur <= d_to:
        wend = min(cur + dt.timedelta(days=6), d_to)
        try:
            found = list_docs(cur, wend)
        except RateLimited as e:
            print(f"[KIND] 요청 제한으로 중단: {e}", flush=True)
            return 1
        for acptno, title in found:
            c = classify(title)
            if c and acptno not in done:
                todo.append((acptno, c[0], c[1]))
        cur = wend + dt.timedelta(days=1)
    todo = sorted(set(todo), reverse=True)        # 최근 문서부터 받는다
    if max_docs and len(todo) > max_docs:
        print(f"[KIND] 대상 {len(todo)}건 중 최근 {max_docs}건만 받는다", flush=True)
        todo = todo[:max_docs]
    print(f"[KIND] 신규 문서 {len(todo)}건 ({d_from} ~ {d_to})", flush=True)
    if not todo:
        return 0

    ex_rows, od_rows = load_csv(EXEC_CSV), load_csv(ORD_CSV)
    fails = 0

    def flush():
        save_csv(EXEC_CSV, ex_rows, EXEC_COLS)
        save_csv(ORD_CSV, od_rows, ORD_COLS)
        DOCS_JSON.write_text(json.dumps(done), encoding="utf-8")

    limited = None
    for i, item in enumerate(todo, 1):
        acptno, market, typ = item
        try:
            rows = parse_doc(fetch_doc_html(acptno), market, acptno)
        except RateLimited as e:
            limited = str(e)
            break
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  실패 {acptno}: {e}", flush=True)
            continue
        (ex_rows if typ == "exec" else od_rows).extend(rows)
        done[acptno] = {"m": market, "t": typ,
                        "d": rows[0]["date"] if rows else "", "n": len(rows)}
        if i % 40 == 0:
            print(f"  {i}/{len(todo)} ({done[acptno]['d']})", flush=True)
            flush()
    if limited:
        flush()
        print(f"[KIND] 요청 제한으로 중단: {limited}. 남은 문서 {len(todo) - i + 1}건은 다음 실행에서 이어 받는다.", flush=True)
        return 1
    flush()
    print(f"[KIND] 완료: 체결 {len(ex_rows)}행, 신청 {len(od_rows)}행, 실패 {fails}건", flush=True)
    return fails


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--from", dest="d_from")
    ap.add_argument("--to", dest="d_to")
    ap.add_argument("--pace", type=float, default=PACE, help="요청 간 최소 간격(초)")
    ap.add_argument("--max-docs", type=int, default=0, help="한 번에 받을 문서 수 상한(0=제한 없음)")
    a = ap.parse_args()
    PACE = a.pace
    d_to = dt.date.fromisoformat(a.d_to) if a.d_to else dt.date.today()
    d_from = dt.date.fromisoformat(a.d_from) if a.d_from else d_to - dt.timedelta(days=a.days)
    sys.exit(1 if run(d_from, d_to, a.max_docs) else 0)
