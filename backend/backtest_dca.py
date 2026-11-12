# -*- coding: utf-8 -*-
"""
定投策略回测：固定定投 vs 系数定投 vs 系数定投+分批止盈 vs 买入持有

方法（严格无前视）:
  每个时点 t 只用 t 及之前的数据计算综合分位、定投系数与止盈档位；
  再按周频模拟四种资金投入方式，用 XIRR（资金加权年化）与时间加权最大回撤比较。

策略:
  A 固定定投   每周投 1 单位
  B 系数定投   每周投 dca 单位（0~1.5，含股债收益差调节）
  C 系数+止盈  同 B，但触发止盈档位时按累计比例卖出，并暂停定投；回落至 65% 以下恢复
  D 买入持有   首个有效时点一次性投入 1 单位

样本: 全部宽基指数，2016-11 以来约 10 年周频数据；不含股息与交易成本。
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DB_PATH", str(Path(__file__).resolve().parent.parent / "db" / "valuation.db"))

import numpy as np
import pandas as pd

import data_service as ds
import valuation as v

MIN_POINTS = 4 * v.PTS_PER_YEAR
TIER_FRAC = {1: 0.25, 2: 0.50, 3: 1.00}


def align_y10(df: pd.DataFrame) -> np.ndarray:
    """把日频国债收益率对齐到周频；缺失向前填充"""
    y = ds.load_cn10y()
    if y is None or y.empty:
        return np.full(len(df), np.nan)
    d = df.assign(date=pd.to_datetime(df["date"])).sort_values("date")
    yy = y.assign(date=pd.to_datetime(y["date"])).sort_values("date")
    m = pd.merge_asof(d, yy, on="date", direction="backward", tolerance=pd.Timedelta(days=10))
    arr = pd.Series(m["y10"].to_numpy(dtype=float)).ffill().bfill().to_numpy()
    return arr


def rolling_signals(df: pd.DataFrame, y10: np.ndarray, index_type: str, cat: str) -> pd.DataFrame:
    """逐时点计算 composite / dca / tier（复刻生产口径，含背离与 ERP 修正）"""
    n = len(df)
    pe = df["pe_ttm"].to_numpy(dtype=float)
    pb = df["pb"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    e = close / pe
    roe = pb / pe
    base = v.BASE_W_PE.get(index_type, 0.50)
    sell_t1 = v.SELL_TIERS.get(index_type, v.SELL_TIERS["balanced"])[0]
    erp_on = cat == v.ERP_CAT and index_type in v.ERP_TYPES and not np.isnan(y10).all()
    erp_arr = (100.0 / pe - y10) if erp_on else None

    comp = np.full(n, np.nan)
    dcas = np.full(n, np.nan)
    tiers = np.zeros(n, dtype=int)
    streak = 0
    for i in range(20, n):
        pe_pct = float((pe[: i + 1] <= pe[i]).mean())
        pb_pct = float((pb[: i + 1] <= pb[i]).mean())

        g = None
        if i >= v.PTS_PER_YEAR and e[i - v.PTS_PER_YEAR] > 0:
            g1 = e[i] / e[i - v.PTS_PER_YEAR] - 1
            g3 = None
            if i >= 3 * v.PTS_PER_YEAR and e[i - 3 * v.PTS_PER_YEAR] > 0:
                g3 = (e[i] / e[i - 3 * v.PTS_PER_YEAR]) ** (1 / 3) - 1
            g = g1 if g3 is None else v.SMOOTH_W1 * g1 + v.SMOOTH_W3 * g3
        w_pe, w_pb = v.dynamic_weights(g, base)
        c = w_pe * pe_pct + w_pb * pb_pct

        if pe_pct - pb_pct >= v.DIVERGE_GAP and pe_pct >= v.DIVERGE_PE_MIN:
            m3 = float(np.nanmean(roe[max(0, i - 3 * v.PTS_PER_YEAR + 1): i + 1]))
            if not (roe[i] >= m3):
                w_pe = max(v.W_PE_MIN, round(w_pe - v.DIVERGE_W_SHIFT, 3))
                w_pb = round(1 - w_pe, 3)
                c = w_pe * pe_pct + w_pb * pb_pct

        boost = 1.0
        if erp_on and not np.isnan(erp_arr[i]):
            lo = max(0, i - v.ERP_YEARS * v.PTS_PER_YEAR)
            ep = float((erp_arr[lo: i + 1] <= erp_arr[i]).mean())
            if ep >= v.ERP_HIGH_PCT and c < v.ERP_ZONE_LOW:
                boost = v.ERP_BOOST
            elif ep <= v.ERP_LOW_PCT and c >= v.ERP_ZONE_HIGH:
                boost = v.ERP_CUT

        dca = min(v.DCA_MAX, max(0.0, (v.DCA_STOP - c) / v.DCA_SPAN) * boost)
        streak = streak + 1 if c >= sell_t1 else 0
        tier, _ = v.sell_plan(c, streak, index_type)
        comp[i], dcas[i], tiers[i] = c, dca, tier

    return pd.DataFrame({"date": df["date"].astype(str).str[:10].to_numpy(),
                         "close": close, "composite": comp, "dca": dcas, "tier": tiers})


def xirr(flows: list[tuple[pd.Timestamp, float]]) -> float:
    if len(flows) < 2:
        return float("nan")
    d0 = flows[0][0]

    def npv(r: float) -> float:
        return sum(a / (1 + r) ** ((d - d0).days / 365.0) for d, a in flows)

    lo, hi = -0.999, 10.0
    if npv(lo) * npv(hi) > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def run_strategy(sig: pd.DataFrame, mode: str) -> dict:
    """mode: fixed / dca / dca_sell / hold"""
    price = sig["close"].to_numpy(dtype=float)
    comp = sig["composite"].to_numpy(dtype=float)
    dca = sig["dca"].to_numpy(dtype=float)
    tier = sig["tier"].to_numpy(dtype=int)
    dates = pd.to_datetime(sig["date"])

    units = cash = invested = 0.0
    sold_frac = 0.0
    held_weeks = 0
    flows: list[tuple[pd.Timestamp, float]] = []
    nav = peak = 1.0
    mdd = 0.0
    prev_val = 0.0
    start = int(np.argmax(~np.isnan(comp)))

    for i in range(start, len(sig)):
        inflow = 0.0
        if mode in ("dca_sell", "dca_sell_keep", "cycle") and tier[i] > 0:
            target = TIER_FRAC[int(tier[i])]
            if target > sold_frac + 1e-9 and units > 0:
                sell_units = units * (target - sold_frac) / (1 - sold_frac)
                cash += sell_units * price[i]
                units -= sell_units
                sold_frac = target
                if mode == "cycle" and units <= 1e-12:
                    sold_frac = 0.0   # 清仓后重置档位，等待下一轮低估再入场
        if mode == "hold":
            amt = 1.0 if i == start else 0.0
        elif mode == "fixed":
            amt = 1.0                      # 固定定投不受止盈信号影响
        elif mode == "dca":
            amt = float(dca[i])
        elif mode == "dca_sell":
            amt = float(dca[i]) if tier[i] == 0 else 0.0
        elif mode == "dca_sell_keep":
            amt = float(dca[i])            # 止盈减仓但不停投（成长型备选方案）
        elif mode == "cycle":
            # 低估/合理偏低才买入，高估卖出，合理偏高持有不动
            amt = float(dca[i]) if comp[i] < v.ZONE_MID else 0.0
        else:
            amt = 0.0
        if amt > 0:
            units += amt / price[i]
            invested += amt
            inflow = amt
            flows.append((dates.iloc[i], -amt))

        val = units * price[i] + cash
        if units > 0:
            held_weeks += 1
        if prev_val > 0:
            r = (val - inflow) / prev_val - 1
            nav *= (1 + r)
            peak = max(peak, nav)
            mdd = min(mdd, nav / peak - 1)
        prev_val = val

    final_val = units * price[-1] + cash
    flows.append((dates.iloc[-1], final_val))
    return {"invested": invested, "value": final_val,
            "xirr": xirr(flows), "mdd": mdd,
            "hold_ratio": held_weeks / max(1, len(sig) - start)}


def main():
    rows = []
    for code, meta in ds.INDICES.items():
        if meta["cat"] != "broad":
            continue
        df = ds.load_series(code)
        if df is None or len(df) < MIN_POINTS:
            print(f"[跳过] {meta['name']}: 数据不足")
            continue
        df = df.reset_index(drop=True)
        y10 = align_y10(df)
        sig = rolling_signals(df, y10, meta["type"], meta["cat"])
        r = {"name": meta["name"], "years": round(len(df) / v.PTS_PER_YEAR, 1)}
        for mode, key in (("fixed", "A"), ("dca", "B"), ("dca_sell", "C"), ("hold", "D")):
            res = run_strategy(sig, mode)
            r[key] = res["xirr"]
            r[key + "_mdd"] = res["mdd"]
            r[key + "_inv"] = res["invested"]
            r[key + "_val"] = res["value"]
        if meta["type"] == "growth":
            r["E"] = run_strategy(sig, "dca_sell_keep")["xirr"]
        cyc = run_strategy(sig, "cycle")
        r["F"], r["F_hold"], r["F_inv"], r["F_val"] = (
            cyc["xirr"], cyc["hold_ratio"], cyc["invested"], cyc["value"])
        r["avg_tier3"] = int((sig["tier"] == 3).sum())
        rows.append(r)

    out = pd.DataFrame(rows).sort_values("A", ascending=False)
    print(f"\n{'=' * 96}")
    print("定投策略回测 · XIRR 年化收益率（无股息、无交易成本）")
    print(f"{'-' * 96}")
    print(f"{'指数':<10}{'年数':>5}{'A 固定':>10}{'B 系数':>10}{'C 系数+止盈':>13}{'D 买入持有':>12}{'C-A':>9}{'C最大回撤':>10}")
    print("-" * 96)
    for _, r in out.iterrows():
        print(f"{r['name']:<10}{r['years']:>5.1f}{r['A']:>10.2%}{r['B']:>10.2%}"
              f"{r['C']:>13.2%}{r['D']:>12.2%}{r['C'] - r['A']:>+9.2%}{r['C_mdd']:>10.1%}")

    print("-" * 96)
    print(f"{'平均':<10}{'':>5}{out['A'].mean():>10.2%}{out['B'].mean():>10.2%}"
          f"{out['C'].mean():>13.2%}{out['D'].mean():>12.2%}{out['C'].mean() - out['A'].mean():>+9.2%}"
          f"{out['C_mdd'].mean():>10.1%}")
    print(f"{'中位数':<10}{'':>5}{out['A'].median():>10.2%}{out['B'].median():>10.2%}"
          f"{out['C'].median():>13.2%}{out['D'].median():>12.2%}{out['C'].median() - out['A'].median():>+9.2%}")

    n = len(out)
    print(f"\n样本 {n} 个宽基指数；C 跑赢 A: {(out['C'] > out['A']).sum()}/{n}"
          f"；B 跑赢 A: {(out['B'] > out['A']).sum()}/{n}")
    print(f"C 最大回撤均值 {out['C_mdd'].mean():.1%}，A 最大回撤均值 {out['A_mdd'].mean():.1%}")

    g = out.dropna(subset=["E"]) if "E" in out.columns else out.iloc[0:0]
    if len(g):
        print("\n成长型对照：E = 止盈时减仓但不停投（C 为减仓并暂停定投）")
        for _, r in g.iterrows():
            print(f"  {r['name']:<8} B 系数 {r['B']:>6.2%} | C 停投 {r['C']:>6.2%} | E 不停投 {r['E']:>6.2%}")

    print(f"\n{'=' * 96}")
    print("周期策略 F：综合分位 <40% 才买入，≥高估线分批卖出，清仓后等回落再入场")
    print(f"{'-' * 96}")
    print(f"{'指数':<10}{'B 持续定投':>12}{'B 期末/投入':>12}{'F 低估买高估卖':>16}{'F 持仓时间':>12}{'F 期末/投入':>12}")
    print("-" * 96)
    for _, r in out.iterrows():
        rb = r["B_val"] / r["B_inv"] if r["B_inv"] else float("nan")
        rf = r["F_val"] / r["F_inv"] if r["F_inv"] else float("nan")
        print(f"{r['name']:<10}{r['B']:>12.2%}{rb:>12.2f}{r['F']:>16.2%}{r['F_hold']:>12.0%}{rf:>12.2f}")
    print(f"{'平均':<10}{out['B'].mean():>12.2%}{out['F'].mean():>16.2%}"
          f"{out['F_hold'].mean():>12.0%}{'':>10}{'':>11}")
    print("\n注: F 的 XIRR 只统计实际投入的资金，空仓期不计入，故偏高；"
          "\n    需结合「持仓时间」与「期末/投入」一起看，持仓时间越低越依赖择时准确性")
    print("\n说明: XIRR 为资金加权年化, 适合分批投入; 回撤为时间加权净值口径;"
          "\n      止盈卖出后资金留存现金、不再投入, 档位累计不重置; 结果作方向参考而非收益承诺")


if __name__ == "__main__":
    main()
