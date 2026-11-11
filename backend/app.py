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

import threading
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import data_service as ds
import valuation

WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"

app = FastAPI(title="指数估值看板", version="1.0.0")

_mem_cache: dict[str, dict] = {}   # code -> ValuationResult.to_dict()


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
    result = valuation.compute(code, meta["name"], df, meta["type"]).to_dict()
    result["category"] = meta["cat"]
    result["category_label"] = ds.CATEGORY_LABEL.get(meta["cat"], "")
    result["updated_at"] = ds.last_updated(code)
    _mem_cache[code] = result
    return result


@app.on_event("startup")
def startup():
    def warm():
        try:
            ds.refresh_all(force=False)
        except Exception:
            pass
        _mem_cache.clear()
    threading.Thread(target=warm, daemon=True).start()


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
            "history_years", "stale_days")})
    if zone and zone != "all":
        out = [x for x in out if x.get("zone") == zone]
    keys = {"composite": lambda x: x.get("composite", 9),
            "composite_desc": lambda x: -x.get("composite", -9),
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
    return {"data": result}


@app.get("/")
def home():
    return FileResponse(WEB_INDEX)


@app.get("/health")
def health():
    return {"ok": True}
