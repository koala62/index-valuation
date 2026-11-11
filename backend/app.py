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
    name, _, itype = ds.INDICES[code]
    result = valuation.compute(code, name, df, itype).to_dict()
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
def list_indices():
    out = []
    for code, (name, *_) in ds.INDICES.items():
        try:
            r = _compute_one(code)
            out.append({k: r[k] for k in (
                "symbol", "name", "type_label", "date", "close", "pe_ttm", "pb",
                "pe_percentile", "pb_percentile", "earnings_g",
                "base_w_pe", "w_pe", "w_pb", "composite", "zone", "distorted")})
        except HTTPException:
            out.append({"symbol": code, "name": name, "error": "no_data"})
    return {"data": out}


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
