# -*- coding: utf-8 -*-
"""
估值查看服务 API
================
GET  /api/indices            全部指数估值摘要
GET  /api/indices/{code}     单指数详情(含10年综合百分位历史)
POST /api/refresh            强制刷新数据
GET  /                       网页版前端
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import data_service as ds
import valuation

WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"
# 随仓库持久化的数据文件：容器重启后运行库被清空时，先从仓库副本恢复
SEED_DB = Path(__file__).resolve().parent.parent / "db" / "valuation.db"
RUN_DB = Path(os.environ.get("DB_PATH", "valuation.db"))
# 每日定时任务：刷新数据并把 SQLite 回传仓库做持久化（Token 只需 repo 权限）
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "koala62/index-valuation")

app = FastAPI(title="指数估值看板", version="1.0.0")

_mem_cache: dict[str, dict] = {}   # code -> ValuationResult.to_dict()
_y10_lock = threading.Lock()
_y10_cache: dict = {"df": None}


def _y10():
    """10年期国债收益率序列；缺数据时懒加载一次，失败不阻塞估值"""
    if _y10_cache["df"] is not None:
        return _y10_cache["df"]
    with _y10_lock:
        if _y10_cache["df"] is None:
            df = ds.load_cn10y()
            if df is None:
                try:
                    ds.save_cn10y(ds.fetch_cn10y())
                    df = ds.load_cn10y()
                except Exception:
                    df = None
            _y10_cache["df"] = df
    return _y10_cache["df"]


def _compute_one(code: str) -> dict:
    if code in _mem_cache:
        return _mem_cache[code]
    df = ds.load_series(code)
    if df is None:
        # 冷启动/新容器：该指数尚无缓存，同步抓取一次
        try:
            ds.save_series(code, ds.fetch_index(code))
        except Exception as e:  # noqa
            raise HTTPException(503, f"{code} 数据获取失败: {e}")
        df = ds.load_series(code)
    if df is None:
        raise HTTPException(404, f"{code} 暂无数据，请先调用 /api/refresh")
    meta = ds.INDICES[code]
    result = valuation.compute(code, meta["name"], df, meta["type"],
                               cat=meta["cat"], y10=_y10()).to_dict()
    result["category"] = meta["cat"]
    result["category_label"] = ds.CATEGORY_LABEL.get(meta["cat"], "")
    result["updated_at"] = ds.last_updated(code)
    _mem_cache[code] = result
    return result


def upload_db_to_github() -> str:
    """把当前 SQLite 提交到仓库 db/valuation.db，供重启后恢复"""
    if not GH_TOKEN:
        return "skip:no_token"
    if not RUN_DB.exists():
        return "skip:no_db"
    api = f"https://api.github.com/repos/{GH_REPO}/contents/db/valuation.db"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "User-Agent": "index-valuation",
               "Accept": "application/vnd.github+json"}
    sha = None
    try:
        with urllib.request.urlopen(urllib.request.Request(api, headers=headers), timeout=30) as r:
            sha = json.load(r).get("sha")
    except Exception:
        pass  # 文件尚不存在时直接新建
    body = {"message": f"data: 每日估值数据刷新 {datetime.now():%Y-%m-%d}",
            "content": base64.b64encode(RUN_DB.read_bytes()).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(api, data=json.dumps(body).encode(), method="PUT",
                                 headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return f"ok:{json.load(r)['commit']['sha'][:7]}"


def _get_meta(key: str) -> str:
    import sqlite3
    with sqlite3.connect(RUN_DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


def _set_meta(key: str, value: str):
    import sqlite3
    with sqlite3.connect(RUN_DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
        conn.commit()


def _daily_loop():
    """每个工作日 18:05 后刷新一次并回传仓库；休眠唤醒后也会补做当天的任务。

    完成日期写在数据库里并随回传持久化：回传触发自动部署后，
    新容器从仓库副本恢复时能看到"今天已跑过"，避免 提交→部署→再跑 的死循环。
    """
    while True:
        now = datetime.now()
        today = now.date().isoformat()
        if now.weekday() < 5 and (now.hour, now.minute) >= (18, 5) and _get_meta("last_daily") != today:
            try:
                ds.refresh_all(force=True)
                _mem_cache.clear()
                _set_meta("last_daily", today)
                upload_db_to_github()
            except Exception:
                time.sleep(600)   # 失败10分钟后重试
        time.sleep(300)


@app.on_event("startup")
def startup():
    if not RUN_DB.exists() and SEED_DB.exists():
        RUN_DB.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SEED_DB, RUN_DB)

    def warm():
        try:
            ds.refresh_all(force=False)
        except Exception:
            pass
        _mem_cache.clear()
    threading.Thread(target=warm, daemon=True).start()
    threading.Thread(target=_daily_loop, daemon=True).start()


@app.get("/api/indices")
def list_indices(category: str | None = None, zone: str | None = None,
                 sort: str = "composite"):
    """category: broad|strategy|industry；zone: 低估|合理偏低|合理偏高|高估；
    sort: composite|composite_desc|pe|pb|name"""
    out = []
    for code, meta in ds.INDICES.items():
        if category and category != "all" and meta["cat"] != category:
            continue
        try:
            r = _compute_one(code)
        except HTTPException:
            out.append({"symbol": code, "name": meta["name"], "category": meta["cat"],
                        "category_label": ds.CATEGORY_LABEL.get(meta["cat"], ""),
                        "error": "no_data"})
            continue
        out.append({k: r[k] for k in (
            "symbol", "name", "category", "category_label", "type_label", "date", "close",
            "pe_ttm", "pb", "pe_percentile", "pb_percentile", "earnings_g",
            "base_w_pe", "w_pe", "w_pb", "composite", "zone", "distorted",
            "history_years", "stale_days", "divergence", "roe", "roe_mean3",
            "roe_percentile", "dca", "pe_norm", "high_weeks", "sell_signal",
            "sell_tier", "reduce_pct", "erp", "erp_percentile", "erp_boost")})
    if zone and zone != "all":
        out = [x for x in out if x.get("zone") == zone]
    keys = {"composite": lambda x: x.get("composite") if x.get("composite") is not None else 9,
            "composite_desc": lambda x: -(x.get("composite") if x.get("composite") is not None else -9),
            "pe": lambda x: x.get("pe_ttm", 9e9),
            "pb": lambda x: x.get("pb", 9e9),
            "name": lambda x: x.get("name", "")}
    return {"data": sorted(out, key=keys.get(sort, keys["composite"]))}


@app.get("/api/categories")
def categories():
    counts: dict[str, int] = {}
    for meta in ds.INDICES.values():
        counts[meta["cat"]] = counts.get(meta["cat"], 0) + 1
    return {"data": [{"key": k, "label": ds.CATEGORY_LABEL[k], "count": counts.get(k, 0)}
                     for k in ds.CATEGORY_LABEL], "total": len(ds.INDICES)}


@app.get("/api/indices/{code}")
def index_detail(code: str):
    code = code.upper()
    if code not in ds.INDICES:
        raise HTTPException(404, "不支持的指数")
    return _compute_one(code)


@app.post("/api/refresh")
def refresh():
    result = ds.refresh_all(force=True)
    _mem_cache.clear()
    _y10_cache["df"] = None
    return {"data": result}


@app.get("/api/admin/db")
def download_db():
    """下载当前 SQLite 数据库，供定时任务回传仓库做持久化"""
    if not RUN_DB.exists():
        raise HTTPException(404, "数据库尚未生成")
    return FileResponse(RUN_DB, filename="valuation.db",
                        media_type="application/octet-stream")


@app.get("/")
def home():
    return FileResponse(WEB_INDEX)


@app.get("/health")
def health():
    return {"ok": True}
