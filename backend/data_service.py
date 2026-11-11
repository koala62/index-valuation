# -*- coding: utf-8 -*-
"""
数据服务：蛋卷基金估值API (10年PE/PB周频历史) + 新浪指数日线(收盘价)
结果缓存到 SQLite，每日自动刷新。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, date

import pandas as pd
import requests

DB_PATH = os.environ.get("DB_PATH", "valuation.db")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Referer": "https://danjuanfunds.com/djmodule/value-center?channel=1300100141"}

# 主流宽基指数: code -> (名称, 新浪代码, 类型)
# 类型: value=价值/周期(金融占比高,PB更可靠) balanced=均衡 growth=成长(PE更敏感)
INDICES = {
    "SH000300": ("沪深300",  "sh000300", "balanced"),
    "SH000905": ("中证500",  "sh000905", "balanced"),
    "SH000852": ("中证1000", "sh000852", "growth"),
    "SH000016": ("上证50",   "sh000016", "value"),
    "SZ399006": ("创业板指", "sz399006", "growth"),
    "SH000688": ("科创50",   "sh000688", "growth"),
    "SH000922": ("中证红利", "sh000922", "value"),
    "SZ399001": ("深证成指", "sz399001", "growth"),
    "SH000010": ("上证180",  "sh000010", "value"),
    "SH000903": ("中证100",  "sh000903", "value"),
}

_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS series(
        symbol TEXT PRIMARY KEY, payload TEXT, updated_at TEXT)""")
    return conn


def _get(url: str, retries: int = 3) -> dict:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa
            last = e
            time.sleep(1 + i)
    raise RuntimeError(f"请求失败 {url}: {last}")


def _danjuan_history(code: str, kind: str) -> pd.DataFrame:
    """kind: 'pe' | 'pb'，返回 date/kind 两列"""
    url = f"https://danjuanfunds.com/djapi/index_eva/{kind}_history/{code}?day=all"
    data = _get(url)["data"][f"index_eva_{kind}_growths"]
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.strftime("%Y-%m-%d")
    return df[["date", kind]].dropna()


SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=3000")


def _sina_close(sina_code: str) -> pd.DataFrame:
    """新浪指数日线收盘价（近 3000 个交易日，约 12 年）"""
    r = requests.get(SINA_KLINE.format(sym=sina_code), headers=UA, timeout=20)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError(f"新浪返回空数据: {sina_code}")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df[["date", "close"]].dropna()


def fetch_index(code: str) -> pd.DataFrame:
    """合并 PE/PB/收盘价，返回 date,close,pe_ttm,pb 升序 DataFrame"""
    name, sina_code, _ = INDICES[code]
    pe = _danjuan_history(code, "pe")
    pb = _danjuan_history(code, "pb")
    df = pe.merge(pb, on="date", how="inner").rename(columns={"pe": "pe_ttm"})
    close = _sina_close(sina_code)
    # 蛋卷为周频，按日期对齐最近一个交易日收盘价
    df["d"] = pd.to_datetime(df["date"])
    close["d"] = pd.to_datetime(close["date"])
    df = pd.merge_asof(df.sort_values("d"), close[["d", "close"]].sort_values("d"),
                       on="d", direction="nearest", tolerance=pd.Timedelta("4D"))
    df = df.dropna(subset=["close"]).drop(columns=["d"]).reset_index(drop=True)
    # 只保留近10年
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=365 * 10 + 5)
    df = df[pd.to_datetime(df["date"]) >= cutoff].reset_index(drop=True)
    if len(df) < 50:
        raise ValueError(f"{name} 合并后数据不足: {len(df)}")
    return df


def save_series(code: str, df: pd.DataFrame):
    payload = json.dumps(df.to_dict(orient="list"), ensure_ascii=False)
    with _lock, _db() as conn:
        conn.execute("INSERT OR REPLACE INTO series VALUES(?,?,?)",
                     (code, payload, datetime.now().isoformat()))


def load_series(code: str) -> pd.DataFrame | None:
    with _lock, _db() as conn:
        row = conn.execute("SELECT payload FROM series WHERE symbol=?", (code,)).fetchone()
    if not row:
        return None
    return pd.DataFrame(json.loads(row[0]))


def last_updated(code: str) -> str | None:
    with _lock, _db() as conn:
        row = conn.execute("SELECT updated_at FROM series WHERE symbol=?", (code,)).fetchone()
    return row[0] if row else None


def is_stale(code: str) -> bool:
    ts = last_updated(code)
    if not ts:
        return True
    return datetime.fromisoformat(ts).date() < date.today()


def refresh_all(force: bool = False) -> dict:
    """刷新全部指数，返回 {code: 'ok'|'skip'|error}"""
    result = {}
    for code in INDICES:
        if not force and not is_stale(code):
            result[code] = "skip"
            continue
        try:
            df = fetch_index(code)
            save_series(code, df)
            result[code] = f"ok({len(df)})"
        except Exception as e:  # noqa
            result[code] = f"err:{type(e).__name__}:{str(e)[:80]}"
        time.sleep(0.5)  # 避免请求过快
    return result
