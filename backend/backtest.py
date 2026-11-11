# -*- coding: utf-8 -*-
"""
样本外回测: 检验综合百分位分档与未来收益的关系在不同市场环境下是否稳定

方法（严格无前视）:
  对每个历史时点 t，仅用 t 及之前的数据(扩展窗口)计算:
    平滑g -> 动态权重 -> 综合百分位 -> 分档
  再统计其后 1年(52期)/3年(156期) 的实际收益。

样本划分:
  IS  样本内: < 2022-01-01  (含2018熊市、2019-2021结构牛)
  OOS 样本外: >= 2022-01-01 (含2022熊市、2024-2025行情)

检验标准: 分档收益应单调递减(低估买未来收益最高)，且IS/OOS方向一致。
"""
from __future__ import annotations

import data_service as ds
import valuation as v
import pandas as pd

BUCKETS = ["低估(<20%)", "合理偏低(20-50%)", "合理偏高(50-80%)", "高估(>=80%)"]
OOS_BOUNDARY = "2022-01-01"
MIN_POINTS = 4 * v.PTS_PER_YEAR   # 至少4年数据的指数才纳入回测


def bucket_of(x: float) -> str:
    if x < 0.20:
        return BUCKETS[0]
    if x < 0.50:
        return BUCKETS[1]
    if x < 0.80:
        return BUCKETS[2]
    return BUCKETS[3]


def simulate(df: pd.DataFrame, index_type: str) -> pd.DataFrame:
    """逐时点无前视模拟综合百分位"""
    e = df["close"] / df["pe_ttm"]
    base = v.BASE_W_PE.get(index_type, 0.50)
    rows = []
    n = len(df)
    for t in range(2 * v.PTS_PER_YEAR, n):
        pe_pct = (df["pe_ttm"].iloc[: t + 1] <= df["pe_ttm"].iloc[t]).mean()
        pb_pct = (df["pb"].iloc[: t + 1] <= df["pb"].iloc[t]).mean()
        g, _, _ = v.earnings_growth(e.iloc[: t + 1])
        w_pe, w_pb = v.dynamic_weights(g, base)
        rows.append((str(df["date"].iloc[t])[:10],
                     w_pe * pe_pct + w_pb * pb_pct,
                     float(df["close"].iloc[t])))
    return pd.DataFrame(rows, columns=["date", "composite", "close"])


def main():
    frames = []
    for code, (name, _, itype) in ds.INDICES.items():
        df = ds.load_series(code)
        if df is None or len(df) < MIN_POINTS:
            print(f"[跳过] {name}: 数据不足({0 if df is None else len(df)}期)")
            continue
        bt = simulate(df, itype)
        bt["symbol"] = name
        frames.append(bt)
        print(f"[纳入] {name}: {len(bt)} 个时点")
    allbt = pd.concat(frames, ignore_index=True)
    allbt["bucket"] = allbt["composite"].map(bucket_of)
    allbt["sample"] = (allbt["date"] >= OOS_BOUNDARY).map({False: "IS", True: "OOS"})

    for horizon, label in [(v.PTS_PER_YEAR, "未来1年"), (3 * v.PTS_PER_YEAR, "未来3年")]:
        allbt["fwd"] = allbt.groupby("symbol")["close"].shift(-horizon) / allbt["close"] - 1
        d = allbt.dropna(subset=["fwd"])
        print(f"\n{'=' * 78}\n{label}收益 · 按综合百分位分档\n{'-' * 78}")
        print(f"{'分档':<14} | {'IS: n/均值/中位/胜率':^28} | {'OOS: n/均值/中位/胜率':^28}")
        print("-" * 78)
        for b in BUCKETS:
            cells = []
            for s in ("IS", "OOS"):
                sub = d[(d["bucket"] == b) & (d["sample"] == s)]["fwd"]
                if len(sub):
                    cells.append(f"{len(sub):>4}  {sub.mean():>+7.1%}  {sub.median():>+7.1%}  {(sub > 0).mean():>5.0%}")
                else:
                    cells.append("   --      --      --    --")
            print(f"{b:<14} | {cells[0]:^28} | {cells[1]:^28}")

        # 单调性检验
        for s in ("IS", "OOS"):
            means = []
            for b in BUCKETS:
                sub = d[(d["bucket"] == b) & (d["sample"] == s)]["fwd"]
                means.append(sub.mean() if len(sub) >= 5 else None)
            valid = [m for m in means if m is not None]
            mono = all(valid[i] >= valid[i + 1] for i in range(len(valid) - 1)) if len(valid) >= 3 else None
            txt = "单调递减 ✓" if mono else ("非单调 ✗" if mono is False else "样本不足")
            print(f"  {s} 分档均值单调性: {txt}  {[f'{m:+.1%}' if m is not None else '--' for m in means]}")

    print("\n说明: 周频数据, fwd为点位涨幅(不含股息); 各指数时点已汇总, 样本量偏小, 结果作稳健性参考而非统计证明")


if __name__ == "__main__":
    main()
