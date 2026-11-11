# -*- coding: utf-8 -*-
"""
数据服务：蛋卷基金估值API (10年PE/PB周频历史) + 东财/新浪指数日线(收盘价)
结果缓存到 SQLite，每日自动刷新。

行情源策略: 东方财富优先(历史完整、单次请求), 新浪兜底(部分指数东财无数据);
两源都取到就选覆盖最新且回溯更早的一份，避免"行情提前中断"导致估值失真。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date

import pandas as pd
import requests

DB_PATH = os.environ.get("DB_PATH", "valuation.db")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Referer": "https://danjuanfunds.com/djmodule/value-center?channel=1300100141"}
EM_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 指数分类: broad=宽基, strategy=策略/红利, industry=行业
CATEGORY_LABEL = {"broad": "宽基", "strategy": "策略红利", "industry": "行业"}

# 指数类型(决定 PE/PB 基准权重): value=价值/周期, balanced=均衡, growth=成长
# em=东方财富 secid, sina=新浪代码(兜底)
INDICES: dict[str, dict] = {
    # ---------- 宽基 ----------
    "SH000300": dict(name="沪深300", cat="broad", type="balanced", em="1.000300", sina="sh000300"),
    "SH000905": dict(name="中证500", cat="broad", type="balanced", em="1.000905", sina="sh000905"),
    "SH000852": dict(name="中证1000", cat="broad", type="growth", em="1.000852", sina="sh000852"),
    "SH000016": dict(name="上证50", cat="broad", type="value", em="1.000016", sina="sh000016"),
    "SZ399006": dict(name="创业板指", cat="broad", type="growth", em="0.399006", sina="sz399006"),
    "SH000688": dict(name="科创50", cat="broad", type="growth", em=None, sina="sh000688"),
    "SZ399001": dict(name="深证成指", cat="broad", type="balanced", em="0.399001", sina="sz399001"),
    "SH000010": dict(name="上证180", cat="broad", type="value", em="1.000010", sina="sh000010"),
    "SH000903": dict(name="中证100", cat="broad", type="value", em="1.000903", sina="sh000903"),
    "SZ399330": dict(name="深证100", cat="broad", type="balanced", em="0.399330", sina="sz399330"),
    "SZ399317": dict(name="国证A指", cat="broad", type="balanced", em="0.399317", sina="sz399317"),
    # ---------- 策略/红利 ----------
    "SH000922": dict(name="中证红利", cat="strategy", type="value", em="1.000922", sina="sh000922"),
    "SH000015": dict(name="上证红利", cat="strategy", type="value", em="1.000015", sina="sh000015"),
    "CSIH30269": dict(name="红利低波", cat="strategy", type="value", em="2.H30269", sina=None),
    "CSI930740": dict(name="300红利低波", cat="strategy", type="value", em="2.930740", sina=None),
    "CSI931157": dict(name="红利成长LV", cat="strategy", type="value", em="2.931157", sina=None),
    "CSI930782": dict(name="500低波", cat="strategy", type="value", em="2.930782", sina=None),
    "SH000919": dict(name="300价值", cat="strategy", type="value", em="1.000919", sina="sh000919"),
    "SH000925": dict(name="基本面50", cat="strategy", type="value", em="1.000925", sina="sh000925"),
    "SZ399324": dict(name="深证红利", cat="strategy", type="value", em="0.399324", sina="sz399324"),
    "SZ399701": dict(name="基本面60", cat="strategy", type="value", em="0.399701", sina="sz399701"),
    "SZ399702": dict(name="基本面120", cat="strategy", type="value", em="0.399702", sina="sz399702"),
    "CSIH30094": dict(name="消费红利", cat="strategy", type="balanced", em="2.H30094", sina=None),
    # ---------- 行业 ----------
    "SZ399997": dict(name="中证白酒", cat="industry", type="balanced", em="0.399997", sina="sz399997"),
    "SZ399396": dict(name="国证食品", cat="industry", type="balanced", em="0.399396", sina="sz399396"),
    "SH000932": dict(name="主要消费", cat="industry", type="balanced", em="1.000932", sina="sh000932"),
    "SZ399989": dict(name="中证医疗", cat="industry", type="growth", em="0.399989", sina="sz399989"),
    "SH000991": dict(name="全指医药", cat="industry", type="growth", em="1.000991", sina="sh000991"),
    "SH000978": dict(name="医药100", cat="industry", type="growth", em="1.000978", sina="sh000978"),
    "SZ399986": dict(name="中证银行", cat="industry", type="value", em="0.399986", sina="sz399986"),
    "SZ399975": dict(name="证券公司", cat="industry", type="value", em="0.399975", sina="sz399975"),
    "SZ399393": dict(name="国证地产", cat="industry", type="value", em="0.399393", sina="sz399393"),
    "SZ399998": dict(name="中证煤炭", cat="industry", type="value", em="0.399998", sina="sz399998"),
    "SH000827": dict(name="中证环保", cat="industry", type="growth", em="1.000827", sina="sh000827"),
    "SH000993": dict(name="全指信息", cat="industry", type="growth", em="1.000993", sina="sh000993"),
    "SH000989": dict(name="全指可选", cat="industry", type="balanced", em="1.000989", sina="sh000989"),
    "SZ399610": dict(name="TMT50", cat="industry", type="growth", em="0.399610", sina="sz399610"),
    "CSI931079": dict(name="5G通信", cat="industry", type="growth", em="2.931079", sina=None),
    "CSI930652": dict(name="中证电子", cat="industry", type="growth", em="2.930652", sina=None),
    "CSI931087": dict(name="科技龙头", cat="industry", type="growth", em="2.931087", sina=None),
    "SZ399967": dict(name="中证军工", cat="industry", type="growth", em="0.399967", sina="sz399967"),
    "SZ399971": dict(name="中证传媒", cat="industry", type="growth", em="0.399971", sina="sz399971"),
    "SZ399812": dict(name="养老产业", cat="industry", type="balanced", em="0.399812", sina="sz399812"),
    "SZ399417": dict(name="新能源车", cat="industry", type="growth", em="0.399417", sina="sz399417"),
    "CSIH30533": dict(name="中国互联网50", cat="industry", type="growth", em="2.H30533", sina=None),
}

_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS series(
        symbol TEXT PRIMARY KEY, payload TEXT, updated_at TEXT)""")
    return conn


def _get(url: str, headers: dict | None = None, retries: int = 3) -> dict:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers or UA, timeout=20)
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


EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
            "&fields1=f1,f2,f3,f4,f5&fields2=f51,f53&klt=101&fqt=1&beg=20150101&end=20500101")

SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=3000")


def _em_close(secid: str) -> pd.DataFrame:
    """东方财富指数日线收盘价（2015年至今）"""
    if not secid:
        raise ValueError("无东财代码")
    d = _get(EM_KLINE.format(secid=secid), headers=EM_UA).get("data")
    if not d or not d.get("klines"):
        raise ValueError(f"东财返回空数据: {secid}")
    rows = [k.split(",")[:2] for k in d["klines"]]
    df = pd.DataFrame(rows, columns=["date", "close"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna().reset_index(drop=True)


def _sina_close(sina_code: str) -> pd.DataFrame:
    """新浪指数日线收盘价（近 3000 个交易日）"""
    if not sina_code:
        raise ValueError("无新浪代码")
    r = requests.get(SINA_KLINE.format(sym=sina_code), headers=UA, timeout=20)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError(f"新浪返回空数据: {sina_code}")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df[["date", "close"]].dropna().reset_index(drop=True)


def _close_series(meta: dict) -> pd.DataFrame:
    """
    取收盘价序列：东财优先、新浪兜底。
    两个源都成功时，选"结束日期最新、起始日期更早"的一份，
    防止某个源中途断更(如新浪中证红利只到2019)污染估值。
    """
    cands, errors = [], []
    for fn, arg in ((_em_close, meta.get("em")), (_sina_close, meta.get("sina"))):
        if not arg:
            continue
        try:
            df = fn(arg)
            if len(df) > 50:
                cands.append(df)
        except Exception as e:  # noqa
            errors.append(f"{fn.__name__}:{type(e).__name__}")
    if not cands:
        raise ValueError(f"行情获取失败 ({', '.join(errors) or '无可用数据源'})")
    return max(cands, key=lambda df: (df["date"].iloc[-1], df["date"].iloc[0]))


def fetch_index(code: str) -> pd.DataFrame:
    """合并 PE/PB/收盘价，返回 date,close,pe_ttm,pb 升序 DataFrame"""
    meta = INDICES[code]
    pe = _danjuan_history(code, "pe")
    pb = _danjuan_history(code, "pb")
    df = pe.merge(pb, on="date", how="inner").rename(columns={"pe": "pe_ttm"})
    close = _close_series(meta)
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
        raise ValueError(f"{meta['name']} 合并后数据不足: {len(df)}")
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


def _refresh_one(code: str, force: bool) -> str:
    if not force and not is_stale(code):
        return "skip"
    try:
        df = fetch_index(code)
        save_series(code, df)
        return f"ok({len(df)})"
    except Exception as e:  # noqa
        return f"err:{type(e).__name__}:{str(e)[:80]}"


def refresh_all(force: bool = False, max_workers: int = 6) -> dict:
    """并发刷新全部指数，返回 {code: 'ok'|'skip'|error}"""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {code: pool.submit(_refresh_one, code, force) for code in INDICES}
        return {code: fut.result() for code, fut in futures.items()}
