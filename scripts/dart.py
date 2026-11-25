"""OpenDART 에서 자기주식 취득 프로그램(직접취득 결정, 신탁계약 체결·해지)을 모은다.

 - list.json (주요사항보고 B) 으로 최근 공시를 훑어 대상 기업을 찾고
 - 구조화 API 3종으로 금액·기간·목적·1일 한도를 가져온다.
     tsstkAqDecsn          자기주식 취득 결정(직접)
     tsstkAqTrctrCnsDecsn  신탁계약 체결 결정
     tsstkAqTrctrCcDecsn   신탁계약 해지 결정
 - list.json (기타공시 E) 에서 결과보고서 제출 여부를 확인한다.
결과: data/dart_programs.json, data/dart_reports.json, data/corp_map.json

사용법:  py scripts/dart.py --days 15              (일일 갱신)
         py scripts/dart.py --days 520             (초기 구축)
         py scripts/dart.py --codes 105560,055550  (특정 종목 강제 재조회)
         py scripts/dart.py --fill-missing         (KIND 에 나오는데 공시를 못 붙인 종목 보충)
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROG_JSON = DATA / "dart_programs.json"
REP_JSON = DATA / "dart_reports.json"
CORP_JSON = DATA / "corp_map.json"
BASE = "https://opendart.fss.or.kr/api"
KEY = (os.environ.get("DART_API_KEY") or os.environ.get("OPENDART_API_KEY") or "").strip()
STRUCT_LOOKBACK = 1100          # 구조화 API 조회 범위(일)
S = requests.Session()

DECISION = {"자기주식취득결정": "direct",
            "자기주식취득신탁계약체결결정": "trust",
            "자기주식취득신탁계약해지결정": "trust_end"}
EP = {"direct": "tsstkAqDecsn", "trust": "tsstkAqTrctrCnsDecsn",
      "trust_end": "tsstkAqTrctrCcDecsn"}
REPORTS = ["자기주식취득결과보고서", "신탁계약해지결과보고서",
           "신탁계약에의한취득상황보고서", "자기주식처분결과보고서"]


def api(path, **params):
    params["crtfc_key"] = KEY
    for i in range(4):
        try:
            r = S.get(f"{BASE}/{path}", params=params, timeout=30)
            j = r.json()
            if j.get("status") == "020":          # 사용한도 초과
                raise SystemExit("DART 사용한도 초과")
            return j
        except (requests.RequestException, ValueError):
            time.sleep(1.5 * (i + 1))
    return {"status": "ERR"}


def num(s):
    t = str(s or "").replace(",", "").strip()
    if t in ("", "-"):
        return None
    try:
        return int(float(t))
    except ValueError:
        return None


def pdate(s):
    v = re.findall(r"\d+", str(s or ""))
    if len(v) >= 3:
        try:
            return dt.date(int(v[0]), int(v[1]), int(v[2])).isoformat()
        except ValueError:
            return None
    return None


def corp_map(refresh=False):
    """stock_code -> {corp_code, name}"""
    if CORP_JSON.exists() and not refresh:
        age = time.time() - CORP_JSON.stat().st_mtime
        if age < 7 * 86400:
            return json.loads(CORP_JSON.read_text(encoding="utf-8"))
    r = S.get(f"{BASE}/corpCode.xml", params={"crtfc_key": KEY}, timeout=120)
    root = ET.parse(zipfile.ZipFile(io.BytesIO(r.content)).open("CORPCODE.xml")).getroot()
    m = {}
    for it in root.findall("list"):
        sc = (it.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = {"corp_code": it.findtext("corp_code").strip(),
                     "name": (it.findtext("corp_name") or "").strip()}
    CORP_JSON.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return m


def scan_list(d_from, d_to, ty):
    """90일 단위로 list.json 순회 → 공시 dict 리스트"""
    out, cur = [], d_from
    while cur <= d_to:
        wend = min(cur + dt.timedelta(days=89), d_to)
        page = 1
        while True:
            j = api("list.json", bgn_de=cur.strftime("%Y%m%d"), end_de=wend.strftime("%Y%m%d"),
                    pblntf_ty=ty, page_no=page, page_count=100)
            if j.get("status") != "000":
                break
            out += j.get("list", [])
            if page >= int(j.get("total_page", 1)):
                break
            page += 1
            time.sleep(0.05)
        cur = wend + dt.timedelta(days=1)
    return out


def norm_program(rec, typ):
    base = {"rcept_no": rec["rcept_no"], "corp_code": rec["corp_code"],
            "corp_name": rec.get("corp_name", ""), "type": typ,
            "filed": "%s-%s-%s" % (rec["rcept_no"][:4], rec["rcept_no"][4:6], rec["rcept_no"][6:8])}
    if typ == "direct":
        base.update({
            "start": pdate(rec.get("aqexpd_bgd")), "end": pdate(rec.get("aqexpd_edd")),
            "amount_ostk": num(rec.get("aqpln_prc_ostk")), "amount_estk": num(rec.get("aqpln_prc_estk")),
            "shares_ostk": num(rec.get("aqpln_stk_ostk")), "shares_estk": num(rec.get("aqpln_stk_estk")),
            "daily_limit_ostk": num(rec.get("d1_prodlm_ostk")),
            "daily_limit_estk": num(rec.get("d1_prodlm_estk")),
            "purpose": (rec.get("aq_pp") or "").strip(), "method": (rec.get("aq_mth") or "").strip(),
            "broker": (rec.get("cs_iv_bk") or "").strip(), "decided": pdate(rec.get("aq_dd")),
        })
    elif typ == "trust":
        base.update({
            "start": pdate(rec.get("ctr_pd_bgd")), "end": pdate(rec.get("ctr_pd_edd")),
            "amount": num(rec.get("ctr_prc")),
            "purpose": (rec.get("ctr_pp") or "").strip(),
            "broker": (rec.get("ctr_cns_int") or rec.get("cs_iv_bk") or "").strip(),
            "decided": pdate(rec.get("bddd")),
        })
    else:
        base.update({
            "start": pdate(rec.get("ctr_pd_bfcc_bgd")), "end": pdate(rec.get("ctr_pd_bfcc_edd")),
            "amount_before": num(rec.get("ctr_prc_bfcc")), "amount_after": num(rec.get("ctr_prc_atcc")),
            "end_date": pdate(rec.get("cc_prd")), "purpose": (rec.get("cc_pp") or "").strip(),
            "broker": (rec.get("cc_int") or "").strip(),
        })
    return base


def fetch_corp(corp_code, today):
    out = []
    bgn = (today - dt.timedelta(days=STRUCT_LOOKBACK)).strftime("%Y%m%d")
    for typ, ep in EP.items():
        j = api(f"{ep}.json", corp_code=corp_code, bgn_de=bgn, end_de=today.strftime("%Y%m%d"))
        if j.get("status") == "000":
            out += [norm_program(r, typ) for r in j.get("list", [])]
        time.sleep(0.04)
    return out


def missing_codes(cmap, progs, today):
    """최근 KIND 에 나오는데 DART 프로그램을 아직 못 붙인 회사를 찾는다(7일에 한 번만 재조회)."""
    import csv
    kind_csv, chk_p = DATA / "kind_exec.csv", DATA / "dart_checked.json"
    if not kind_csv.exists():
        return []
    checked = json.loads(chk_p.read_text()) if chk_p.exists() else {}
    have = {(p["corp_code"], p["type"]) for p in progs.values()}
    since = (today - dt.timedelta(days=45)).isoformat()
    out = set()
    with open(kind_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["date"] < since or r["side"] != "매수":
                continue
            code = r["code"] if r["code"] in cmap else r["code"][:5] + "0"
            if code not in cmap:
                continue
            cc = cmap[code]["corp_code"]
            typ = "direct" if r["kind"] == "직접" else "trust"
            last = checked.get(cc)
            if (cc, typ) not in have and (not last or (today - dt.date.fromisoformat(last)).days >= 7):
                out.add(code)
                checked[cc] = today.isoformat()
    chk_p.write_text(json.dumps(checked))
    return sorted(out)


def run(days, codes, fill_missing=False):
    if not KEY:
        raise SystemExit("환경변수 DART_API_KEY 가 필요합니다.")
    DATA.mkdir(exist_ok=True)
    today = dt.date.today()
    cmap = corp_map()
    cc2sc = {v["corp_code"]: k for k, v in cmap.items()}
    progs = json.loads(PROG_JSON.read_text(encoding="utf-8")) if PROG_JSON.exists() else {}
    reports = json.loads(REP_JSON.read_text(encoding="utf-8")) if REP_JSON.exists() else {}

    d_from = today - dt.timedelta(days=days)
    targets = set()
    for it in scan_list(d_from, today, "B"):
        nm = it.get("report_nm", "").replace(" ", "")
        if any(k in nm for k in DECISION) and it.get("stock_code", "").strip():
            targets.add(it["corp_code"])
    print(f"[DART] 주요사항보고 스캔: 대상 기업 {len(targets)}곳 ({d_from} ~ {today})", flush=True)

    for it in scan_list(d_from, today, "E"):
        nm = it.get("report_nm", "").replace(" ", "")
        kind = next((k for k in REPORTS if k in nm), None)
        if kind and it.get("stock_code", "").strip():
            reports[it["rcept_no"]] = {"corp_code": it["corp_code"], "code": it["stock_code"].strip(),
                                       "name": it.get("corp_name", ""), "kind": kind,
                                       "date": "%s-%s-%s" % (it["rcept_dt"][:4], it["rcept_dt"][4:6], it["rcept_dt"][6:8]),
                                       "correction": "정정" in nm}
    if fill_missing:
        extra = missing_codes(cmap, progs, today)
        print(f"[DART] 프로그램 미확보 {len(extra)}개 종목 추가 조회", flush=True)
        codes = list(codes) + extra
    for c in codes:
        if c in cmap:
            targets.add(cmap[c]["corp_code"])

    for i, cc in enumerate(sorted(targets), 1):
        for p in fetch_corp(cc, today):
            p["code"] = cc2sc.get(cc, "")
            progs[p["rcept_no"]] = p
        if i % 100 == 0:
            print(f"  {i}/{len(targets)}", flush=True)
            PROG_JSON.write_text(json.dumps(progs, ensure_ascii=False), encoding="utf-8")

    PROG_JSON.write_text(json.dumps(progs, ensure_ascii=False, indent=0), encoding="utf-8")
    REP_JSON.write_text(json.dumps(reports, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"[DART] 프로그램 {len(progs)}건, 결과보고서 {len(reports)}건 저장", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--codes", default="")
    ap.add_argument("--fill-missing", action="store_true")
    a = ap.parse_args()
    run(a.days, [c for c in a.codes.split(",") if c], a.fill_missing)
