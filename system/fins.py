"""財務の point-in-time 化。

既存検証 (jquants/backtest_pit.FinState と backtest_all_pit.Fin) と同じ計算を移植し、
開示ごとの状態を「使える日 (avail)」から次の開示まで日付 x 銘柄の行列に広げる。

使える日: 開示日の翌営業日。15:00 以降の開示は翌々営業日 (既存検証と同じ、保守側)。
1行ごとに3つの日時を持つ: 公表 (DiscDate+DiscTime)・利用開始 (avail の営業日)・取得 (retrieved_at)。
"""
from __future__ import annotations

import bisect
import math

import numpy as np
import pandas as pd

QN = {"1Q": 1, "2Q": 2, "3Q": 3, "FY": 4}
# 株価に依らない値 (株価が要る PER・配当利回り・時価総額は行列にしてから計算する)
FIELDS = ("eps_ttm", "np_ttm", "eq", "div", "rgf", "egf", "shares", "er", "op_ttm", "sales_ttm",
          "rg", "eg", "opg", "npf_growth")


def fnum(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def prev_fy(fy):
    try:
        y, m, d = fy.split("-")
        return f"{int(y) - 1}-{m}-{d}"
    except (ValueError, AttributeError):
        return None


class FinState:
    def __init__(self):
        self.stmts = {}      # (FY末, 期) -> {Sales, NP, EPS, OP}
        self.last = None
        self.eq = None
        self.fc = {}         # FY末 -> {Sales, EPS, Div, NP} 会社予想 (最新)
        self.div_act = None
        self.cur_fy = None
        self.shares = None
        self.er = None
        self.cut = False     # この開示で純利益予想を10%以上下方修正したか

    # backtest_pit.FinState.add + backtest_all_pit.Fin.add
    def add(self, r):
        self.cut = False
        dt_ = r.get("DocType", "")
        fy = r.get("CurFYEn", "")
        if "FinancialStatements" in dt_ and r.get("CurPerType") in QN:
            per = r["CurPerType"]
            key = (fy, per)
            self.stmts[key] = {"Sales": fnum(r.get("Sales")), "NP": fnum(r.get("NP")), "EPS": fnum(r.get("EPS")),
                               "OP": fnum(r.get("OP"))}
            if self.last is None or (fy, QN[per]) >= (self.last[0], QN[self.last[1]]):
                self.last = key
                if fnum(r.get("Eq")) is not None:
                    self.eq = fnum(r.get("Eq"))
                sh, tr = fnum(r.get("ShOutFY")), fnum(r.get("TrShFY")) or 0.0
                if sh:
                    self.shares = sh - tr
                if fnum(r.get("EqAR")) is not None:
                    self.er = fnum(r.get("EqAR")) * 100
            if per == "FY":
                if fnum(r.get("DivAnn")) is not None:
                    self.div_act = fnum(r.get("DivAnn"))
                nfy = r.get("NxtFYEn")
                if nfy:
                    self.cur_fy = max(self.cur_fy or "", nfy)
                    self._fc(nfy, r.get("NxFSales"), r.get("NxFEPS"), r.get("NxFDivAnn"), r.get("NxFNp"))
            else:
                self.cur_fy = max(self.cur_fy or "", fy)
                self._fc(fy, r.get("FSales"), r.get("FEPS"), r.get("FDivAnn"), r.get("FNP"))
        elif "Revision" in dt_ and fy:
            self._fc(fy, r.get("FSales"), r.get("FEPS"), r.get("FDivAnn"), r.get("FNP"))

    def _fc(self, fy, sales, eps, div, np_):
        d = self.fc.setdefault(fy, {})
        old = d.get("NP")
        new = fnum(np_)
        if old is not None and old > 0 and new is not None and new / old - 1 <= -0.10:
            self.cut = True
        for k, v in (("Sales", sales), ("EPS", eps), ("Div", div), ("NP", np_)):
            v = fnum(v)
            if v is not None:
                d[k] = v

    def ttm(self, key):
        if not self.last:
            return None
        fy, per = self.last
        cur = self.stmts.get((fy, per), {}).get(key)
        if per == "FY":
            return cur
        pfy = prev_fy(fy)
        full = self.stmts.get((pfy, "FY"), {}).get(key)
        prev_ytd = self.stmts.get((pfy, per), {}).get(key)
        if cur is not None and full is not None and prev_ytd is not None:
            return full + cur - prev_ytd
        if full is not None:
            return full
        return cur * 4 / QN[per] if cur is not None else None

    def yoy(self, key):
        if not self.last:
            return None
        fy, per = self.last
        cur = self.stmts.get((fy, per), {}).get(key)
        prv = self.stmts.get((prev_fy(fy), per), {}).get(key)
        if cur is None or prv is None or prv <= 0:
            return None
        return (cur / prv - 1) * 100

    def snapshot(self) -> dict:
        v = dict.fromkeys(FIELDS)
        v["eps_ttm"], v["np_ttm"], v["op_ttm"], v["sales_ttm"] = (self.ttm(k) for k in ("EPS", "NP", "OP", "Sales"))
        v["eq"], v["shares"], v["er"] = self.eq, self.shares, self.er
        fy = self.cur_fy
        fc = self.fc.get(fy, {}) if fy else {}
        v["div"] = fc.get("Div", self.div_act)
        base_s = self.stmts.get((prev_fy(fy), "FY"), {}).get("Sales") if fy else None
        if fc.get("Sales") and base_s and base_s > 0:
            v["rgf"] = (fc["Sales"] / base_s - 1) * 100
        elif self.last:
            v["rgf"] = self.yoy("Sales")
        base_e = self.stmts.get((prev_fy(fy), "FY"), {}).get("EPS") if fy else None
        if fc.get("EPS") is not None and base_e is not None and base_e > 0:
            v["egf"] = (fc["EPS"] / base_e - 1) * 100
        base_n = self.stmts.get((prev_fy(fy), "FY"), {}).get("NP") if fy else None
        if fc.get("NP") is not None and base_n is not None and base_n > 0:
            v["npf_growth"] = (fc["NP"] / base_n - 1) * 100
        v["rg"], v["eg"], v["opg"] = self.yoy("Sales"), self.yoy("NP"), self.yoy("OP")
        return v


def avail_index(dates: list[str], disc_date: str, disc_time: str) -> int:
    k = bisect.bisect_right(dates, disc_date)
    if (disc_time or "00:00") >= "15:00":
        k += 1
    return k


class FinPanel:
    """FIELDS ごとの日付 x 銘柄の行列と、下方修正のあった日 (cut)・決算短信のある銘柄 (has_stmt)"""

    def __init__(self, dates: list[str], codes: list[str], fins: pd.DataFrame):
        nd, nc = len(dates), len(codes)
        cidx = {c: j for j, c in enumerate(codes)}
        self.m = {f: np.full((nd, nc), np.nan) for f in FIELDS}
        self.cut = np.zeros((nd, nc), dtype=bool)
        self.has_stmt = np.zeros(nc, dtype=bool)
        df = fins[fins["Code"].isin(cidx)].copy()
        df["k"] = [avail_index(dates, d, t) for d, t in zip(df["DiscDate"], df["DiscTime"])]
        df = df.sort_values(["Code", "k", "DiscDate", "DiscTime"], kind="stable")
        for code, g in df.groupby("Code", sort=False):
            j = cidx[code]
            st = FinState()
            recs = g.to_dict("records")
            n = len(recs)
            for q, r in enumerate(recs):
                st.add(r)
                k = r["k"]
                if "FinancialStatements" in r.get("DocType", ""):
                    self.has_stmt[j] = True
                if st.cut and k < nd:
                    self.cut[k, j] = True
                if q + 1 < n and recs[q + 1]["k"] == k:
                    continue  # 同じ日に使えるようになる開示は、全部取り込んでから
                if k >= nd:
                    break
                nxt = recs[q + 1]["k"] if q + 1 < n else nd
                snap = st.snapshot()
                for f in FIELDS:
                    if snap[f] is not None:
                        self.m[f][k:min(nxt, nd), j] = snap[f]

    def price_values(self, C: np.ndarray) -> dict:
        """当時の実株価から PER・配当利回り・時価総額 (億円)・ROE・営業利益率"""
        eps = self.m["eps_ttm"]
        with np.errstate(divide="ignore", invalid="ignore"):
            per = np.where(eps > 0, C / eps, np.where(np.isnan(eps), np.nan, -1.0))
            per[np.isnan(C)] = np.nan
            eq = self.m["eq"]
            roe = np.where(eq > 0, self.m["np_ttm"] / eq * 100, np.nan)
            dy = self.m["div"] / C * 100
            mc = C * self.m["shares"] / 1e8
            s = self.m["sales_ttm"]
            opm = np.where(s > 0, self.m["op_ttm"] / s * 100, np.nan)
        return {"per": per, "roe": roe, "dy": dy, "mc": mc, "opm": opm}
