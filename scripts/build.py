"""수집한 원자료(KIND 일별 신청·체결, DART 프로그램, KRX 시세)를 묶어
대시보드용 JSON 을 만든다.

출력:  docs/data/index.json        요약(진행중·최근 종료·당일 변화)
       docs/data/p/<id>.json       프로그램별 일별 시계열

용어
 - 프로그램: 직접취득 1건(종목코드 단위) 또는 신탁계약 1건(회사 단위).
 - 진행률: 직접 = KIND 누적체결량 ÷ 신고수량, 신탁 = 계약 시작 후 체결금액 합 ÷ 계약금액.
 - 시간 경과율: 취득(계약)기간 중 지나간 거래일 비율.
"""
import bisect
import csv
import datetime as dt
import gzip
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "data"
EOK = 1e8
ENDED_KEEP_TD = 40        # 종료 후 이 거래일 수만큼 '최근 종료'에 남긴다
SPARK_TD = 20


# ───────────── 입력 ─────────────
def load_csv(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def I(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def D(s):
    return dt.date.fromisoformat(s) if s else None


def load_px():
    """→ px[code][date] = (close, vol, value), last[code] = 최신 스냅샷, 거래일 목록"""
    px, last, days = defaultdict(dict), {}, []
    for p in sorted((DATA / "px").glob("*.csv.gz")):
        d = dt.date(int(p.name[:4]), int(p.name[4:6]), int(p.name[6:8]))
        days.append(d)
        with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
            r = csv.reader(f)
            next(r)
            for code, name, market, close, vol, value, mktcap, shares in r:
                px[code][d] = (int(close), int(vol), int(value))
                last[code] = {"name": name, "market": market, "close": int(close),
                              "mktcap": int(mktcap), "shares": int(shares), "date": d}
    return px, last, days


def load_holidays():
    p = DATA / "holidays.json"
    return {D(x) for x in json.loads(p.read_text(encoding="utf-8"))} if p.exists() else set()


# ───────────── 거래일 달력 ─────────────
class Cal:
    def __init__(self, past, holidays, ref):
        days = set(past)
        d = max(past) if past else ref
        end = ref + dt.timedelta(days=800)
        while d <= end:
            d += dt.timedelta(days=1)
            if d.weekday() < 5 and d not in holidays:
                days.add(d)
        # 과거 구간 중 시세가 없는 평일(백필 이전)도 거래일로 간주
        d = min(past) if past else ref
        start = d - dt.timedelta(days=1200)
        while d > start:
            d -= dt.timedelta(days=1)
            if d.weekday() < 5 and d not in holidays:
                days.add(d)
        self.days = sorted(days)

    def idx(self, d):                      # d 이하 마지막 거래일의 인덱스
        return bisect.bisect_right(self.days, d) - 1

    def count(self, a, b):                 # a~b 양끝 포함 거래일 수
        if not a or not b or b < a:
            return 0
        return bisect.bisect_right(self.days, b) - bisect.bisect_left(self.days, a)

    def add(self, d, n):                   # d 로부터 n 거래일 뒤(앞)
        i = min(max(self.idx(d) + n, 0), len(self.days) - 1)
        return self.days[i]

    def between(self, a, b):
        return self.days[bisect.bisect_left(self.days, a):bisect.bisect_right(self.days, b)]


def r2(x, nd=2):
    return None if x is None else round(x, nd)


def pct(a, b, nd=1):
    return None if not b or a is None else round(a / b * 100, nd)


# ───────────── 프로그램 구성 ─────────────
def base_code(code, cmap):
    if code in cmap:
        return code
    b = code[:5] + "0"
    return b if b in cmap else code


def pick_latest(recs, key):
    """정정공시 등으로 같은 키가 여럿이면 접수번호가 큰 것만 남긴다."""
    best = {}
    for r in recs:
        k = key(r)
        if k not in best or r["rcept_no"] > best[k]["rcept_no"]:
            best[k] = r
    return sorted(best.values(), key=lambda r: (r.get("start") or "", r["rcept_no"]))


def build_direct(exec_rows, progs_by_corp, cmap, cal, ref):
    """직접취득: 종목코드별로 신고수량이 바뀌는 지점을 새 프로그램으로 끊는다."""
    by_code = defaultdict(list)
    for r in exec_rows:
        if r["kind"] == "직접" and r["side"] == "매수":
            by_code[r["code"]].append(r)
    eps, used = [], set()
    for code, rows in by_code.items():
        rows.sort(key=lambda r: r["date"])
        cur, prev = None, None
        for r in rows:
            d = D(r["date"])
            new = (cur is None or I(r["declared"]) != cur["declared"]
                   or I(r["cum"]) < I(prev["cum"])
                   or cal.count(D(prev["date"]), d) > 45)
            if new:
                cur = {"type": "direct", "code": code, "name": r["name"], "market": r["market"],
                       "declared": I(r["declared"]), "rows": []}
                eps.append(cur)
            cur["rows"].append(r)
            prev = r
    for ep in eps:
        first = D(ep["rows"][0]["date"])
        bc = base_code(ep["code"], cmap)
        is_pref = bc != ep["code"]
        cc = cmap.get(bc, {}).get("corp_code")
        cands = [p for p in progs_by_corp.get(cc, []) if p["type"] == "direct" and p.get("start")
                 and D(p["start"]) - dt.timedelta(days=10) <= first <= (D(p["end"]) if p.get("end") else first)]
        key = "shares_estk" if is_pref else "shares_ostk"
        exact = [p for p in cands if p.get(key) == ep["declared"]]
        prog = (exact or cands or [None])[-1]
        ep["prog"], ep["is_pref"] = prog, is_pref
        if prog:
            used.add(prog["rcept_no"])
    # DART 결정은 있는데 KIND 에 아직 한 번도 안 나온 프로그램(미개시)
    for cc, plist in progs_by_corp.items():
        for p in plist:
            if p["type"] != "direct" or p["rcept_no"] in used or not p.get("start") or not p.get("end"):
                continue
            if not (D(p["start"]) <= ref <= D(p["end"])) or not p.get("code"):
                continue
            if not p.get("shares_ostk"):
                continue
            eps.append({"type": "direct", "code": p["code"], "name": p["corp_name"], "market": "",
                        "declared": p["shares_ostk"], "rows": [], "prog": p, "is_pref": False})
    return eps


def build_trust(exec_rows, progs_by_corp, cmap, cal, ref, data_start):
    """신탁: 회사 단위 계약에 일별 체결을 귀속한다(여러 계약이면 먼저 시작한 계약부터)."""
    by_corp = defaultdict(list)
    for r in exec_rows:
        if r["kind"] == "신탁" and r["side"] == "매수":
            by_corp[base_code(r["code"], cmap)].append(r)
    eps = []
    corps = set(by_corp)
    for cc, plist in progs_by_corp.items():
        for p in plist:
            if p["type"] == "trust" and p.get("code"):
                corps.add(p["code"])
    for bc in corps:
        cc = cmap.get(bc, {}).get("corp_code")
        plist = progs_by_corp.get(cc, [])
        contracts = []
        for p in [x for x in plist if x["type"] == "trust" and x.get("start") and x.get("end")]:
            # 연장: 직전 계약 종료 직후에 같은 금액으로 다시 체결되면 같은 계약으로 본다
            prev = contracts[-1] if contracts else None
            if prev and prev["amount"] == p.get("amount") and \
                    -10 <= (D(p["start"]) - prev["end"]).days <= 10:
                prev["end"] = D(p["end"])
                prev["extended"] = True
                prev["prog"] = {**p, "start": prev["start"].isoformat()}
                continue
            contracts.append({"start": D(p["start"]), "end": D(p["end"]), "amount": p.get("amount") or 0,
                              "prog": p, "term": None, "extended": False})
        for t in [x for x in plist if x["type"] == "trust_end"]:
            for c in contracts:
                if t.get("start") and D(t["start"]) == c["start"] and t.get("end_date"):
                    c["term"] = D(t["end_date"])
                    c["term_reason"] = t.get("purpose", "")
        contracts = [c for c in contracts if (c["term"] or c["end"]) >= data_start - dt.timedelta(days=5)]
        for c in contracts:
            c.update({"rows": [], "cum": 0})
        orphan = []
        for r in sorted(by_corp.get(bc, []), key=lambda r: r["date"]):
            d = D(r["date"])
            act = [c for c in contracts if c["start"] <= d <= (c["term"] or c["end"])]
            if not act:                    # 공시상 시작일보다 며칠 먼저/늦게 잡히는 경우
                act = [c for c in contracts if c["start"] - dt.timedelta(days=5) <= d
                       <= (c["term"] or c["end"]) + dt.timedelta(days=5)]
            if not act:
                orphan.append(r)
                continue
            tgt = next((c for c in act if c["cum"] < c["amount"]), act[-1])
            tgt["rows"].append(r)
            tgt["cum"] += I(r["amount"])
        info = by_corp.get(bc, [{}])[0]
        for c in contracts:
            p = c["prog"]
            eps.append({"type": "trust", "code": bc, "name": p["corp_name"] or info.get("name", ""),
                        "market": info.get("market", ""), "rows": c["rows"], "prog": p,
                        "contract": c, "is_pref": False})
        if orphan:
            # 계약 공시를 찾지 못한 신탁 매수: 60거래일 넘게 끊기면 다른 건으로 나눈다
            cur, prev = None, None
            for r in orphan:
                d = D(r["date"])
                if cur is None or cal.count(D(prev["date"]), d) > 60:
                    cur = {"type": "trust", "code": bc, "name": r["name"], "market": r["market"],
                           "rows": [], "prog": None, "contract": None, "is_pref": False}
                    eps.append(cur)
                cur["rows"].append(r)
                prev = r
    return eps


# ───────────── 지표 ─────────────
def metrics(ep, ctx):
    cal, ref, px, last, orders, reports, data_start, next_day = (
        ctx[k] for k in ("cal", "ref", "px", "last", "orders", "reports", "data_start", "next_day"))
    typ, code, prog = ep["type"], ep["code"], ep["prog"]
    rows = ep["rows"]
    codes = sorted({r["code"] for r in rows}) or [code]
    snap = last.get(code, {})
    close = snap.get("close")

    # 기간
    first = D(rows[0]["date"]) if rows else None
    lastx = D(rows[-1]["date"]) if rows else None
    start = D(prog["start"]) if prog and prog.get("start") else first
    end = D(prog["end"]) if prog and prog.get("end") else None
    end_est = False
    if end is None:                        # 공시 미확보: 직접취득 최장 3개월로 가정
        end = (start + dt.timedelta(days=91)) if typ == "direct" else None
        end_est = True
    if first and start and first < start:
        start = first
    con = ep.get("contract")
    term = con["term"] if con else None

    # 일별 합산(신탁은 보통주·우선주를 합친다)
    day = defaultdict(lambda: {"req": 0, "exec": 0, "amt": 0})
    for r in rows:
        d = D(r["date"])
        req = I(r["req"]) or orders.get((r["date"], r["code"], r["kind"]), {}).get("req", 0)
        if r["code"] == code:
            day[d]["req"] += req
            day[d]["exec"] += I(r["exec"])
        day[d]["amt"] += I(r["amount"])

    cum_amt = sum(v["amt"] for v in day.values())
    cum_sh_main = sum(v["exec"] for v in day.values())
    if typ == "direct":
        target_sh = ep["declared"] or None
        cum_sh = I(rows[-1]["cum"]) if rows else 0
        key = "amount_estk" if ep["is_pref"] else "amount_ostk"
        target_amt = prog.get(key) if prog else None
        progress = pct(cum_sh, target_sh)
        amt_progress = pct(cum_amt, target_amt)
        remain_sh = max((target_sh or 0) - cum_sh, 0)
        remain_amt = remain_sh * close if close else None
        partial = bool(rows) and cum_sh > cum_sh_main * 1.02 + 1
    else:
        target_sh, cum_sh = None, cum_sh_main
        target_amt = con["amount"] if con else None
        progress = pct(cum_amt, target_amt)
        amt_progress = progress
        remain_amt = max(target_amt - cum_amt, 0) if target_amt else None
        remain_sh = int(remain_amt / close) if remain_amt and close else None
        partial = bool(start and start < data_start)
    no_hist = partial and not [r for r in rows if D(r["date"]) >= data_start] and typ == "trust"
    if typ == "direct" and not rows and start and start < data_start:
        no_hist = True

    # 상태
    rep_kinds = {"direct": ("자기주식취득결과보고서",), "trust": ("신탁계약해지결과보고서",)}[typ]
    # 결과보고서: 시작 7일 이후에 제출됐고 그 뒤로 체결이 없을 때만 이 프로그램의 것으로 본다
    bc_reports = [x for x in reports if x["code"] == base_code(code, ctx["cmap"]) and x["kind"] in rep_kinds
                  and start and D(x["date"]) > start + dt.timedelta(days=7)
                  and (lastx is None or D(x["date"]) >= lastx)
                  and (end is None or D(x["date"]) <= end + dt.timedelta(days=20))]
    end_reason, end_date = None, None
    idle0 = cal.count(cal.add(lastx, 1), ref) if lastx and lastx < ref else 0
    if typ == "direct" and target_sh and cum_sh >= target_sh:
        end_reason, end_date = "수량 완료", lastx
    elif (typ == "direct" and not partial and amt_progress is not None and amt_progress >= 99.5
          and (ep.get("later") or idle0 >= 3)):
        # 금액을 정해 놓고 사는 회사는 신고수량을 다 채우기 전에 예정 금액을 다 써서 끝난다
        end_reason, end_date = "금액 소진", lastx
    elif typ == "direct" and bc_reports and not ep.get("later"):
        end_reason, end_date = "결과보고", lastx or D(bc_reports[0]["date"])
    elif term and term <= ref:
        end_reason, end_date = "계약 해지", term
    elif end and end < ref:
        end_reason, end_date = "기간 만료", end
    elif ep.get("later"):                  # 같은 종목에 더 새 프로그램이 시작됨
        end_reason, end_date = "후속 프로그램 시작", lastx
    elif typ == "trust" and prog is None and lastx and cal.count(lastx, ref) > 20:
        end_reason, end_date = "주문 중단", lastx
    if (not end_reason and typ == "trust" and progress is not None and progress >= 97 and idle0 >= 3
            and not orders.get((next_day.isoformat() if next_day else "", code, "신탁"))):
        end_reason, end_date = "금액 소진", lastx
    status = "ended" if end_reason else ("notstarted" if not rows and not no_hist else "active")

    # 시간 경과
    td_total = cal.count(start, end) if start and end else None
    td_elapsed = cal.count(start, min(ref, end)) if start and end else None
    td_left = cal.count(cal.add(ref, 1), end) if end and end > ref else 0
    time_pct = pct(td_elapsed, td_total)
    gap = r2(progress - time_pct, 1) if progress is not None and time_pct is not None else None

    # 거래량·거래대금
    cpx = px.get(code, {})
    pday = max([d for d in cpx if d <= ref], default=None)
    win20 = [cpx[d] for d in cal.between(cal.add(ref, -19), ref) if d in cpx]
    adv_val = sum(x[2] for x in win20) / len(win20) if win20 else None
    adv_vol = sum(x[1] for x in win20) / len(win20) if win20 else None

    def adv_n(n):                          # 최근 n거래일 평균 거래대금(원)
        w = [cpx[d][2] for d in cal.between(cal.add(ref, -(n - 1)), ref) if d in cpx]
        return sum(w) / len(w) if w else None
    adv5, adv60 = adv_n(5), adv_n(60)

    def window(a, b):                      # ref 기준 a~b 거래일 전 구간 (0 = ref)
        return cal.between(cal.add(ref, -b), cal.add(ref, -a))

    w5, w20 = window(0, 4), window(5, 24)
    ex5 = sum(day[d]["exec"] for d in w5 if d in day)
    rq5 = sum(day[d]["req"] for d in w5 if d in day)
    am5 = sum(day[d]["amt"] for d in w5 if d in day)
    vol5 = sum(cpx[d][1] for d in w5 if d in cpx)
    rq20 = sum(day[d]["req"] for d in w20 if d in day)
    r5_share = pct(ex5, vol5)
    fill5 = pct(ex5, rq5)
    avg_rq5, avg_rq20 = rq5 / 5, rq20 / 20
    req_ratio = r2(avg_rq5 / avg_rq20, 2) if avg_rq20 > 0 else None
    r5_amt = am5 / 5

    need_amt = remain_amt / td_left if remain_amt is not None and td_left > 0 else None
    need_vs_adv = pct(need_amt, adv_val)

    # 다음 거래일 신청
    nx = [orders.get((next_day.isoformat(), c, "직접" if typ == "direct" else "신탁")) for c in codes] \
        if next_day else []
    nx_main = orders.get((next_day.isoformat(), code, "직접" if typ == "direct" else "신탁")) if next_day else None
    if status == "ended":                  # 끝난 프로그램에 후속 프로그램의 주문이 붙지 않게 한다
        nx_main = None
    next_req = nx_main["req"] if nx_main else (0 if next_day and status != "ended" else None)
    prev_req = day[ref]["req"] if ref in day else 0
    next_chg = pct(next_req - prev_req, prev_req) if prev_req and next_req is not None else None
    limit = (prog.get("daily_limit_estk" if ep["is_pref"] else "daily_limit_ostk") if prog and typ == "direct" else None)
    limit_use = pct(next_req, limit) if limit and next_req else None

    # 현 속도 유지 시 예상 완료일
    est_finish, slack = None, None
    if status == "active" and remain_amt and r5_amt > 0:
        need_td = int(-(-remain_amt // r5_amt))
        est_finish = cal.add(ref, need_td)
        slack = td_left - need_td

    avg_price = cum_amt / cum_sh_main if cum_sh_main and len(codes) == 1 else None
    p0 = cpx.get(max([d for d in cpx if start and d <= start], default=None)) if cpx else None
    ret_start = pct(close - p0[0], p0[0]) if p0 and close else None
    idle = cal.count(cal.add(lastx, 1), ref) if lastx and lastx < ref else 0

    tags = []
    if no_hist and status == "active":
        tags.append("이력 부족")
        progress = amt_progress = gap = remain_amt = need_amt = need_vs_adv = None
    if status == "notstarted":
        tags.append("미개시")
    if status == "active":
        if first and cal.count(first, ref) <= 5 and not partial:
            tags.append("신규")
        if req_ratio is not None and req_ratio >= 1.5:
            tags.append("신청 증가")
        if req_ratio is not None and req_ratio <= 0.6 and rq5 > 0:
            tags.append("신청 감소")
        if progress is not None and (progress >= 90 or (slack is not None and est_finish and cal.count(cal.add(ref, 1), est_finish) <= 5)):
            tags.append("종료 임박")
        if idle >= 5 and not next_req and rows:
            tags.append("주문 중단")
    if status in ("active", "notstarted") and not no_hist:
        if gap is not None and gap <= -20 and (time_pct or 0) >= 30:
            tags.append("뒤처짐")
        if 0 < td_left <= 10 and (progress or 0) < 90:
            tags.append("기한 임박")

    spark = []
    for d in window(0, SPARK_TD - 1):
        v = day.get(d)
        spark.append([v["req"], v["exec"]] if v else [0, 0])

    purpose = (prog or {}).get("purpose", "")
    pid = f"{code}_{typ[0]}_{(start or ref).strftime('%Y%m%d')}"
    out = {
        "id": pid, "code": code, "name": snap.get("name") or ep["name"], "market": snap.get("market") or ep["market"],
        "type": typ, "status": status, "tags": tags,
        "cancel": "소각" in purpose, "purpose": purpose, "broker": (prog or {}).get("broker", ""),
        "dart": (prog or {}).get("rcept_no"), "filed": (prog or {}).get("filed"),
        "start": start.isoformat() if start else None, "end": end.isoformat() if end else None,
        "end_est": end_est, "extended": bool(con and con.get("extended")), "pref_mixed": len(codes) > 1,
        "mktcap": r2(snap.get("mktcap", 0) / EOK, 0) if snap.get("mktcap") else None, "close": close,
        "target_amt": r2(target_amt / EOK) if target_amt else None, "target_sh": target_sh,
        "target_vs_cap": pct(target_amt, snap.get("mktcap")) if target_amt else None,
        "cum_amt": r2(cum_amt / EOK), "cum_sh": cum_sh,
        "progress": progress, "amt_progress": amt_progress, "time_pct": time_pct, "gap": gap,
        "td_total": td_total, "td_left": td_left,
        "remain_amt": r2(remain_amt / EOK) if remain_amt is not None else None, "remain_sh": remain_sh,
        "need_amt": r2(need_amt / EOK) if need_amt is not None else None, "need_vs_adv": need_vs_adv,
        "adv20": r2(adv_val / EOK) if adv_val else None,
        "adv5": r2(adv5 / EOK) if adv5 else None, "adv60": r2(adv60 / EOK) if adv60 else None,
        "r5_share": r5_share, "r5_amt": r2(r5_amt / EOK), "fill5": fill5, "req_ratio": req_ratio,
        "next_req": next_req, "next_timing": nx_main["timing"] if nx_main else None,
        "prev_req": prev_req, "next_chg": next_chg, "limit": limit, "limit_use": limit_use,
        "est_finish": est_finish.isoformat() if est_finish else None, "slack": slack,
        "avg_price": r2(avg_price, 0), "ret_start": ret_start,
        "first_exec": first.isoformat() if first else None, "last_exec": lastx.isoformat() if lastx else None,
        "idle": idle, "partial": partial, "spark": spark,
    }
    if status == "ended":
        pe = cpx.get(max([d for d in cpx if end_date and d <= end_date], default=None)) if cpx else None
        out.update({"end_reason": end_reason, "end_date": end_date.isoformat() if end_date else None,
                    "td_since_end": cal.count(cal.add(end_date, 1), ref) if end_date and end_date < ref else 0,
                    "ret_end": pct(close - pe[0], pe[0]) if pe and close else None,
                    "px_end": pe[0] if pe else None})
        tot_vol = sum(cpx[d][1] for d in day if d in cpx)
        out["prog_share"] = pct(sum(v["exec"] for d, v in day.items() if d in cpx), tot_vol)

    # 상세 시계열
    s0 = cal.add(start or first or ref, -15)
    s1 = ref if status != "ended" else min(ref, cal.add(end_date or ref, 30))
    series = []
    run = 0
    for d in cal.between(s0, s1):
        v, p = day.get(d), cpx.get(d)
        if v:
            run += v["amt"] if typ == "trust" else v["exec"]
        series.append([d.isoformat(), p[0] if p else None, p[1] if p else None,
                       v["req"] if v else 0, v["exec"] if v else 0, r2(v["amt"] / EOK) if v else 0])
    detail = {"id": pid, "series": series, "cols": ["date", "close", "vol", "req", "exec", "amt"],
              "reports": [{"date": x["date"], "kind": x["kind"], "rcept_no": x["rcept_no"]}
                          for x in reports if x["code"] == base_code(code, ctx["cmap"]) and start
                          and D(x["date"]) >= start]}
    return out, detail


def feed(programs, ended, ref, next_day):
    items = []
    for p in programs:
        if p["status"] == "active" and p["first_exec"] == ref.isoformat() and not p["partial"]:
            items.append({"kind": "start", "id": p["id"], "name": p["name"], "type": p["type"],
                          "text": f"첫 체결 {p['cum_sh']:,}주"})
        if p["status"] == "active" and p["next_req"] is not None and p["prev_req"]:
            ch = p["next_chg"]
            if p["next_req"] == 0:
                items.append({"kind": "stop", "id": p["id"], "name": p["name"], "type": p["type"],
                              "text": f"다음 거래일 신청 없음 (직전 {p['prev_req']:,}주, 진행률 {p['progress']}%)"})
            elif ch is not None and abs(ch) >= 30:
                items.append({"kind": "up" if ch > 0 else "down", "id": p["id"], "name": p["name"], "type": p["type"],
                              "text": f"신청 {p['prev_req']:,} → {p['next_req']:,}주 ({ch:+.0f}%)"})
        if p["status"] == "active" and not p["prev_req"] and p["next_req"]:
            items.append({"kind": "resume" if p["cum_sh"] else "start", "id": p["id"], "name": p["name"],
                          "type": p["type"], "text": f"다음 거래일 신청 {p['next_req']:,}주"})
        if p["status"] == "notstarted" and p.get("filed") and (ref - D(p["filed"])).days <= 7:
            items.append({"kind": "new", "id": p["id"], "name": p["name"], "type": p["type"],
                          "text": f"취득 결정 공시 ({p['target_amt'] or '-'}억, ~{p['end']})"})
    for p in ended:
        if p["end_date"] == ref.isoformat():
            items.append({"kind": "end", "id": p["id"], "name": p["name"], "type": p["type"],
                          "text": f"{p['end_reason']} (진행률 {p['progress']}%)"})
    order = {"start": 0, "new": 1, "up": 2, "resume": 3, "down": 4, "stop": 5, "end": 6}
    items.sort(key=lambda x: (order[x["kind"]], x["name"]))
    return items


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    exec_rows = load_csv(DATA / "kind_exec.csv")
    order_rows = load_csv(DATA / "kind_orders.csv")
    progs = json.loads((DATA / "dart_programs.json").read_text(encoding="utf-8"))
    reports_raw = json.loads((DATA / "dart_reports.json").read_text(encoding="utf-8"))
    cmap = json.loads((DATA / "corp_map.json").read_text(encoding="utf-8"))
    px, last, px_days = load_px()

    exec_dates = sorted({D(r["date"]) for r in exec_rows})
    ref = exec_dates[-1]
    # 기준일에서 거꾸로, 두 시장 체결내역이 빠짐없이 이어지는 구간의 첫날
    docs = json.loads((DATA / "kind_docs.json").read_text(encoding="utf-8"))
    have = defaultdict(set)
    for v in docs.values():
        if v["t"] == "exec" and v["d"]:
            have[D(v["d"])].add(v["m"])
    data_start = ref
    for d in sorted([x for x in set(px_days) | set(exec_dates) if x <= ref], reverse=True):
        if len(have.get(d, ())) < 2:
            break
        data_start = d
    cal = Cal(set(px_days) | set(exec_dates), load_holidays(), ref)
    order_dates = sorted({D(r["date"]) for r in order_rows})
    next_day = order_dates[-1] if order_dates and order_dates[-1] > ref else None

    orders = {}
    for r in order_rows:
        if r["side"] == "매수":
            orders[(r["date"], r["code"], r["kind"])] = {"req": I(r["req"]), "timing": r["timing"]}
    reports = [dict(v, rcept_no=k) for k, v in reports_raw.items() if not v.get("correction")]

    by_corp = defaultdict(list)
    for p in progs.values():
        by_corp[p["corp_code"]].append(p)
    for cc in by_corp:
        by_corp[cc] = pick_latest(by_corp[cc], lambda r: (r["type"], r.get("start"), r.get("end_date")))

    eps = build_direct(exec_rows, by_corp, cmap, cal, ref) + \
        build_trust(exec_rows, by_corp, cmap, cal, ref, data_start)
    # 같은 종목·유형에서 뒤에 시작한 프로그램이 있으면 앞의 것은 종료로 본다(직접취득)
    grp = defaultdict(list)
    for ep in eps:
        if ep["type"] == "direct" and ep["rows"]:
            grp[ep["code"]].append(ep)
    for lst in grp.values():
        lst.sort(key=lambda e: e["rows"][0]["date"])
        for e in lst[:-1]:
            e["later"] = True

    ctx = {"cal": cal, "ref": ref, "px": px, "last": last, "orders": orders, "reports": reports,
           "data_start": data_start, "next_day": next_day, "cmap": cmap}
    # 폴더째 지우면 동기화 프로그램이나 로컬 서버가 폴더를 잡고 있을 때 실패하므로 파일만 지운다
    (OUT / "p").mkdir(parents=True, exist_ok=True)
    for f in (OUT / "p").glob("*.json"):
        f.unlink()
    active, ended, seen = [], [], set()
    for ep in eps:
        if not ep["rows"] and not ep["prog"]:
            continue
        m, detail = metrics(ep, ctx)
        if m["id"] in seen:
            continue
        if m["status"] == "ended":
            if m["td_since_end"] > ENDED_KEEP_TD or not ep["rows"]:
                continue
            ended.append(m)
        else:
            if m["type"] == "trust" and not ep["rows"] and m["end"] and D(m["end"]) < ref:
                continue
            active.append(m)
        seen.add(m["id"])
        (OUT / "p" / f"{m['id']}.json").write_text(
            json.dumps(detail, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    active.sort(key=lambda m: -(m["mktcap"] or 0))
    ended.sort(key=lambda m: (m["end_date"] or "", m["mktcap"] or 0), reverse=True)

    day_exec = [r for r in exec_rows if r["date"] == ref.isoformat() and r["side"] == "매수"]
    meta = {
        "ref": ref.isoformat(), "next_day": next_day.isoformat() if next_day else None,
        "px_date": max(px_days).isoformat() if px_days else None,
        "data_start": data_start.isoformat(),
        "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "day_amt": r2(sum(I(r["amount"]) for r in day_exec) / EOK, 0),
        "day_count": len({r["code"] for r in day_exec}),
        "next_count": len({k[1] for k in orders if next_day and k[0] == next_day.isoformat()}),
        "n_active": sum(1 for m in active if m["status"] == "active"),
        "n_notstarted": sum(1 for m in active if m["status"] == "notstarted"),
        "n_ended": len(ended),
        "remain_amt": r2(sum(m["remain_amt"] or 0 for m in active), 0),
    }
    idx = {"meta": meta, "programs": active, "ended": ended, "feed": feed(active, ended, ref, next_day)}
    (OUT / "index.json").write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[BUILD] 기준일 {ref} · 진행중 {meta['n_active']} · 미개시 {meta['n_notstarted']} · "
          f"최근 종료 {len(ended)} · 변화 {len(idx['feed'])}건 · index.json "
          f"{(OUT / 'index.json').stat().st_size // 1024}KB")


if __name__ == "__main__":
    main()
