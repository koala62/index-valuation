# -*- coding: utf-8 -*-
"""
估值百分位核心算法 v2
=====================
输入: 某指数近10年的周频序列 (date, close, pe_ttm, pb)
输出: PE百分位、PB百分位、动态权重、综合百分位、估值区间、失真提示

v2 改进:
 ① 盈利增速 g 平滑: 0.4×近1年同比 + 0.6×近3年CAGR，降低单期噪音；
    数据不足时逐级回退(仅1年同比 -> 固定基准权重)
 ② PE/PB 基准权重按指数类型预设，g 只做边际微调(最大±12个百分点):
      价值/周期型 base_pe=0.35  (盈利波动大，PB更可靠)
      均衡型     base_pe=0.50
      成长型     base_pe=0.65  (盈利成长性主导定价，PE更敏感)
 ③ 分档参数经样本外回测检验，见 backtest.py

盈利代理: E = 指数点位 / PE_TTM
g < 0 说明盈利趋势走弱，此时 PE_TTM 被动抬高 -> PE百分位失真偏高，
因此下调 PE 权重、上调 PB 权重（PB 对盈利波动不敏感）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd

PTS_PER_YEAR = 52          # 数据为周频
HISTORY_YEARS = 10

# 指数类型 -> PE 基准权重
BASE_W_PE = {"value": 0.35, "balanced": 0.50, "growth": 0.65}
INDEX_TYPE_LABEL = {"value": "价值/周期型", "balanced": "均衡型", "growth": "成长型"}
G_ADJUST_MAX = 0.12        # g 对权重的最大边际调整幅度
G_CAP = 0.25               # 参与调权的 g 封顶
W_PE_MIN, W_PE_MAX = 0.20, 0.80
SMOOTH_W1, SMOOTH_W3 = 0.4, 0.6   # 1年同比 / 3年CAGR 平滑系数
NORM_YEARS = 3             # 正常化PE: 盈利取3年均值，平滑一次性减值/周期波动
DIVERGE_GAP = 0.30         # PE/PB 分位背离阈值(百分点)
DIVERGE_PE_MIN = 0.60      # PE 分位需高于此值才判定为"PE虚高"
DIVERGE_W_SHIFT = 0.20     # 背离时 PE 权重下调幅度


@dataclass
class ValuationResult:
    symbol: str
    name: str
    index_type: str
    type_label: str
    date: str
    close: float
    pe_ttm: float
    pb: float
    pe_percentile: float          # 0~1
    pb_percentile: float          # 0~1
    earnings_g: Optional[float]   # 平滑后盈利增速
    earnings_g1: Optional[float]  # 近1年同比
    earnings_g3: Optional[float]  # 近3年CAGR
    base_w_pe: float              # 类型基准PE权重
    w_pe: float                   # 微调后PE权重
    w_pb: float
    composite: float              # 综合百分位 0~1
    zone: str                     # 低估/合理偏低/合理偏高/高估
    distorted: bool               # 是否存在盈利下滑导致的PE失真
    history_years: float = 0.0    # 历史覆盖年数
    stale_days: int = 0           # 行情滞后天数
    note: str = ""
    history: list = field(default_factory=list)  # 综合百分位历史(采样)
    pe_norm: Optional[float] = None  # 正常化PE(3年平滑盈利)
    divergence: str = ""  # pe_inflated: PE分位高而PB分位低，判定PE虚高

    def to_dict(self):
        return asdict(self)


def _percentile_of(series: pd.Series, value: float) -> float:
    """当前值在历史序列中的百分位 (0~1)"""
    s = series.dropna()
    if len(s) < 20:
        return float("nan")
    return float((s <= value).mean())


def normalized_pe(df: pd.DataFrame, years: int = NORM_YEARS) -> pd.Series:
    """
    正常化PE = 点位 / 近3年平均盈利
    用于剔除一次性减值、周期底部等因素导致的 PE 被动跳变
    """
    e = pd.to_numeric(df["close"] / df["pe_ttm"], errors="coerce").astype(float)
    e = e.replace([math.inf, -math.inf], float("nan"))
    e_s = e.rolling(years * PTS_PER_YEAR, min_periods=PTS_PER_YEAR).mean()
    return df["close"] / e_s


def earnings_growth(e: pd.Series) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    e: 盈利代理序列 (close / pe_ttm)
    返回 (平滑g, 近1年同比, 近3年CAGR)；数据不足逐级回退
    """
    e = e.replace([math.inf, -math.inf], pd.NA).dropna()
    n = len(e)
    g1 = g3 = None
    if n > PTS_PER_YEAR and e.iloc[-1 - PTS_PER_YEAR] > 0:
        g1 = float(e.iloc[-1] / e.iloc[-1 - PTS_PER_YEAR] - 1)
    if n > 3 * PTS_PER_YEAR and e.iloc[-1 - 3 * PTS_PER_YEAR] > 0:
        g3 = float((e.iloc[-1] / e.iloc[-1 - 3 * PTS_PER_YEAR]) ** (1 / 3) - 1)
    if g1 is None:
        return None, None, None
    g = g1 if g3 is None else SMOOTH_W1 * g1 + SMOOTH_W3 * g3
    return g, g1, g3


def dynamic_weights(g: Optional[float], base_w_pe: float) -> tuple[float, float]:
    """类型基准权重 + g 边际微调"""
    if g is None:
        return base_w_pe, round(1 - base_w_pe, 3)
    adj = max(-G_CAP, min(G_CAP, g)) / G_CAP * G_ADJUST_MAX
    w_pe = max(W_PE_MIN, min(W_PE_MAX, base_w_pe + adj))
    return round(w_pe, 3), round(1 - w_pe, 3)


def zone_of(composite: float) -> str:
    if composite < 0.20:
        return "低估"
    if composite < 0.50:
        return "合理偏低"
    if composite < 0.80:
        return "合理偏高"
    return "高估"


def compute(symbol: str, name: str, df: pd.DataFrame,
            index_type: str = "balanced") -> ValuationResult:
    """
    df 需包含列: date(升序), close, pe_ttm, pb  (周频)
    """
    df = df.dropna(subset=["close", "pe_ttm", "pb"]).reset_index(drop=True)
    if len(df) < 100:
        raise ValueError(f"{name} 有效数据不足: {len(df)}")

    cur = df.iloc[-1]
    pe_norm = normalized_pe(df)
    pe_pct = _percentile_of(df["pe_ttm"], cur["pe_ttm"])
    pb_pct = _percentile_of(df["pb"], cur["pb"])

    base = BASE_W_PE.get(index_type, 0.50)
    type_label = INDEX_TYPE_LABEL.get(index_type, "均衡型")
    g, g1, g3 = earnings_growth(df["close"] / df["pe_ttm"])
    w_pe, w_pb = dynamic_weights(g, base)
    w_pe_g = w_pe  # g 微调后的权重（背离修正前），用于文案
    distorted = g is not None and g < 0

    composite = w_pe * pe_pct + w_pb * pb_pct

    # PE/PB 分位背离检测：PE 高而 PB 低，多半是盈利下滑把 PE 抬起来了
    divergence = ""
    if pe_pct - pb_pct >= DIVERGE_GAP and pe_pct >= DIVERGE_PE_MIN:
        divergence = "pe_inflated"
        base_w = w_pe
        w_pe = max(W_PE_MIN, round(w_pe - DIVERGE_W_SHIFT, 3))
        w_pb = round(1 - w_pe, 3)
        composite = w_pe * pe_pct + w_pb * pb_pct

    note = ""
    span_years = (pd.to_datetime(df["date"].iloc[-1]) - pd.to_datetime(df["date"].iloc[0])).days / 365
    stale_days = int((pd.Timestamp.now().normalize() - pd.to_datetime(cur["date"])).days)
    if span_years < 8:
        note = f"历史数据仅约 {span_years:.1f} 年，百分位参考意义有限。"
    if stale_days > 20:
        note += f"行情数据截至 {str(cur['date'])[:10]}，已滞后 {stale_days} 天，估值区间可能失真。"
    if distorted:
        g1t = f"{g1:+.1%}" if g1 is not None else "--"
        g3t = f"{g3:+.1%}" if g3 is not None else "--"
        note += (f"平滑盈利增速 {g:+.1%}（1年{g1t}/3年CAGR {g3t}），PE 存在被动抬高失真，"
                 f"PE 权重在{type_label}基准 {base:.0%} 上微调至 {w_pe_g:.0%}。")
    elif g is not None and g > 0.1:
        note += f"平滑盈利增速 {g:+.1%} 向好，PE 权重在基准 {base:.0%} 上微调至 {w_pe_g:.0%}。"

    pe_norm_val = float(pe_norm.iloc[-1]) if pd.notna(pe_norm.iloc[-1]) else None
    if pe_norm_val and cur["pe_ttm"] > 0:
        gap = pe_norm_val / float(cur["pe_ttm"]) - 1
        if gap < -0.2:
            note += (f"正常化PE {pe_norm_val:.1f} 明显低于原始PE {cur['pe_ttm']:.1f}（{gap:+.0%}），"
                     f"说明近3年盈利被一次性因素压低、PE 被动抬高；图中橙色虚线为正常化口径参考。")

    if divergence == "pe_inflated":
        note += (f"PE 分位 {pe_pct:.0%} 远高于 PB 分位 {pb_pct:.0%}，"
                 f"PE 偏高更可能来自盈利下滑而非股价透支，已将 PE 权重由 {base_w:.0%} 下调至 {w_pe:.0%}。")

    # 综合百分位历史（用于走势图）：用当期权重近似全历史
    history = _composite_history(df, w_pe, w_pb, pe_norm)

    return ValuationResult(
        symbol=symbol, name=name,
        index_type=index_type, type_label=type_label,
        date=str(cur["date"])[:10],
        close=round(float(cur["close"]), 2),
        pe_ttm=round(float(cur["pe_ttm"]), 2),
        pb=round(float(cur["pb"]), 2),
        pe_percentile=round(pe_pct, 4),
        pb_percentile=round(pb_pct, 4),
        earnings_g=None if g is None else round(g, 4),
        earnings_g1=None if g1 is None else round(g1, 4),
        earnings_g3=None if g3 is None else round(g3, 4),
        base_w_pe=base, w_pe=w_pe, w_pb=w_pb,
        composite=round(composite, 4),
        zone=zone_of(composite),
        distorted=distorted,
        history_years=round(span_years, 1),
        stale_days=stale_days,
        note=note,
        history=history,
        pe_norm=None if pe_norm_val is None else round(pe_norm_val, 2),
        divergence=divergence,
    )


def _composite_history(df: pd.DataFrame, w_pe: float, w_pb: float,
                       pe_norm: pd.Series, step: int = 5) -> list:
    """滚动综合百分位历史（全历史区间），按期采样以控制体积

    主口径：原始 PE_TTM + PB 的滚动百分位（与当前估值一致）；
    另附 composite_norm：PE 改用3年平滑盈利的正常化口径，作为参考线，
    用于识别一次性减值等造成的估值尖峰（不改变主口径数值）。
    """
    pb = df["pb"]
    pe = df["pe_ttm"]
    dates = df["date"]
    out = []
    n = len(df)
    idxs = list(range(20, n, step))  # 前20期用于建立百分位基准
    if idxs and idxs[-1] != n - 1:
        idxs.append(n - 1)
    for i in idxs:
        pb_hist = pb.iloc[: i + 1].dropna()
        pe_hist = pe.iloc[: i + 1].dropna()
        if len(pb_hist) < 20 or len(pe_hist) < 20:
            continue
        pe_pct = (pe_hist <= pe.iloc[i]).mean()
        pb_pct = (pb_hist <= pb.iloc[i]).mean()
        row = {
            "date": str(dates.iloc[i])[:10],
            "composite": round(w_pe * pe_pct + w_pb * pb_pct, 4),
            "close": round(float(df["close"].iloc[i]), 2),
        }
        # 正常化口径（参考线）：仅用于识别一次性利润波动，不改变主口径
        pn_hist = pe_norm.iloc[: i + 1].dropna()
        if len(pn_hist) >= 20 and pd.notna(pe_norm.iloc[i]):
            pn_pct = (pn_hist <= pe_norm.iloc[i]).mean()
            row["composite_norm"] = round(w_pe * pn_pct + w_pb * pb_pct, 4)
        out.append(row)
    return out
