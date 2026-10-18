"""Refresh public CB market snapshots without changing the historical backtest.

The peer model and contractual YTM calculation follow the local research v2.
Only a complete, dated, price-consistent snapshot is allowed to replace data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import SplineTransformer, StandardScaler

META_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
VALUE_URL = "https://datacenter-web.eastmoney.com/api/data/get"
ROOT = Path(__file__).resolve().parents[1]
CHINESE_NUMBERS = dict(zip("一二三四五六七八九十", range(1, 11)))


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def date_value(value):
    return pd.to_datetime(value, errors="coerce")


def fetch_source(today):
    session = requests.Session()
    session.trust_env = False
    session.headers["User-Agent"] = "Mozilla/5.0 convertible-bond-pwa/1.0"
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )))

    def get(url, params):
        response = session.get(url, params=params, timeout=(15, 45))
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if not payload.get("success") or not result or not result.get("data"):
            raise RuntimeError(f"Data source returned an empty result: {payload.get('message')}")
        return result

    params = {
        "reportName": "RPT_BOND_CB_LIST", "columns": "ALL", "pageSize": 500,
        "pageNumber": 1, "sortColumns": "PUBLIC_START_DATE", "sortTypes": -1,
        "source": "WEB", "client": "WEB",
        "quoteColumns": (
            "f2~01~CONVERT_STOCK_CODE~CONVERT_STOCK_PRICE,"
            "f235~10~SECURITY_CODE~TRANSFER_PRICE,"
            "f236~10~SECURITY_CODE~TRANSFER_VALUE,"
            "f2~10~SECURITY_CODE~CURRENT_BOND_PRICE,"
            "f237~10~SECURITY_CODE~TRANSFER_PREMIUM_RATIO"
        ),
    }
    first = get(META_URL, params)
    metadata = list(first["data"])
    pages = int(first["pages"])
    if pages > 20:
        raise RuntimeError("Unexpected metadata pagination; refusing a partial universe")
    for page in range(2, pages + 1):
        params["pageNumber"] = page
        metadata.extend(get(META_URL, params)["data"])
    # One large, newest-first page must contain the whole latest session.
    values = get(VALUE_URL, {
        "type": "RPTA_WEB_KZZ_LS", "sty": "ALL", "st": "date", "sr": -1,
        "p": 1, "ps": 5000, "source": "WEB",
        "filter": f"(date>='{(today - timedelta(days=21)).isoformat()}')",
    })["data"]
    print(f"Fetched {len(metadata)} metadata rows and {len(values)} recent valuations", flush=True)
    return {"metadata": metadata, "values": values}


def cashflows(meta):
    start = date_value(meta.get("VALUE_DATE"))
    if pd.isna(start):
        start = date_value(meta.get("LISTING_DATE"))
    maturity = date_value(meta.get("EXPIRE_DATE"))
    if pd.isna(start) or pd.isna(maturity) or maturity <= start:
        return None
    rates = {}
    pattern = r"第\s*([一二三四五六七八九十\d]+)\s*年\s*(?:为|是|:|：)?\s*([\d.]+)\s*%"
    for year, rate in re.findall(pattern, str(meta.get("INTEREST_RATE_EXPLAIN", ""))):
        year = int(year) if year.isdigit() else CHINESE_NUMBERS.get(year)
        if year:
            rates[year] = float(rate)
    if not rates:
        return None
    term = max(rates)
    clause = str(meta.get("REDEEM_CLAUSE", ""))
    match = re.search(r"到期赎回[^%]{0,160}?([\d]+(?:\.[\d]+)?)\s*%", clause)
    if not match:
        match = re.search(r"([\d]+(?:\.[\d]+)?)\s*%\s*[（(]含最后一期利息", clause)
    redemption = float(match.group(1)) if match else 100 + rates[term]
    result = [(start + pd.DateOffset(years=y), rates.get(y, rates[term]))
              for y in range(1, term) if start + pd.DateOffset(years=y) < maturity]
    result.append((maturity, redemption))
    return result


def ytm_pct(price, day, flows):
    if not flows:
        return None
    remaining = [(float((d - day).days) / 365, amount) for d, amount in flows if d > day]
    if not remaining:
        return None

    def pv(rate):
        return sum(amount / (1 + rate) ** years for years, amount in remaining)

    low, high = -0.95, 1.0
    for _ in range(8):
        if pv(high) <= price:
            break
        high = min(100, high * 2 + 1)
    if pv(low) < price or pv(high) > price:
        return None
    for _ in range(40):
        mid = (low + high) / 2
        if pv(mid) > price:
            low = mid
        else:
            high = mid
    return (low + high) * 50


def prepare_frame(source, today):
    values = source["values"]
    latest = max(str(v.get("DATE", ""))[:10] for v in values)
    day = pd.Timestamp(latest)
    age = (today - day.date()).days
    if age < 0 or age > 14:
        raise RuntimeError(f"Source date {latest} is outside the allowed freshness window")
    newest = {str(v["ZCODE"]).zfill(6): v for v in values if str(v.get("DATE", ""))[:10] == latest}
    if len(newest) >= 5000:
        raise RuntimeError("Latest valuation page may be truncated")
    records = []
    expected = 0
    missing = []
    quote_matches = 0
    for meta in source["metadata"]:
        code = str(meta.get("SECURITY_CODE", "")).zfill(6)
        listing = date_value(meta.get("LISTING_DATE"))
        maturity = date_value(meta.get("EXPIRE_DATE"))
        delist = date_value(meta.get("DELIST_DATE"))
        quoted = number(meta.get("CURRENT_BOND_PRICE"))
        if (not code.startswith(("11", "12")) or pd.isna(listing) or listing > day
                or (pd.notna(maturity) and maturity <= day)
                or (pd.notna(delist) and delist <= day)
                or quoted is None or quoted <= 0):
            continue
        expected += 1
        value = newest.get(code)
        if not value:
            missing.append(code)
            continue
        price, conversion, pure, actual = [number(value.get(k)) for k in
                                           ("FCLOSE", "SWAPVALUE", "PUREBONDVALUE", "SWAPOR")]
        if any(v is None for v in (price, conversion, pure, actual)) or min(price, conversion, pure) <= 0:
            missing.append(code)
            continue
        if abs(price - conversion * (1 + actual / 100)) > max(0.02, price * 0.0002):
            raise RuntimeError(f"Price / conversion-premium identity failed for {code}")
        quote_matches += abs(quoted - price) <= max(0.01, price * 0.0001)
        transfer = number(meta.get("TRANSFER_PRICE"))
        underlying = conversion * transfer / 100 if transfer and transfer > 0 else None
        # Snapshot date alignment is checked across the whole quote universe.
        # Underlying value uses the source's same-date conversion value.
        flows = cashflows(meta)
        notices = [date_value(meta.get(k)) for k in ("NOTICE_DATE_HS", "NOTICE_DATE_SH")]
        notice = min((d for d in notices if pd.notna(d)), default=pd.NaT)
        scale = number(meta.get("ACTUAL_ISSUE_SCALE"))
        records.append({
            "bond_code": code, "name": meta.get("SECURITY_NAME_ABBR", code),
            "close": price, "conversion_value": conversion, "pure_bond_value": pure,
            "premium_actual_pct": actual,
            "remaining_years": (maturity - day).days / 365 if pd.notna(maturity) else np.nan,
            "log_issue_scale": math.log1p(max(0, scale)) if scale is not None else np.nan,
            "underlying_close": underlying, "ytm_pct": ytm_pct(price, day, flows),
            "notice_active": bool(pd.notna(notice) and notice <= day),
        })
    if expected < 80 or len(records) < max(80, expected * 0.98):
        raise RuntimeError(f"Insufficient market coverage: {len(records)}/{expected}; missing={missing}")
    if quote_matches / len(records) < 0.95:
        raise RuntimeError("Metadata quotes and historical valuation dates do not align; retry after source settlement")
    frame = pd.DataFrame(records).sort_values("bond_code").reset_index(drop=True)
    print(f"Validated {latest}: {len(frame)}/{expected} quoted active bonds; price alignment {quote_matches}/{len(frame)}", flush=True)
    return day, frame, expected


def fit_model(frame):
    features = ["conversion_value", "pure_bond_value", "remaining_years", "log_issue_scale"]
    sample = frame.loc[frame["conversion_value"].between(5, 1000)].copy()
    if len(sample) < 40:
        raise RuntimeError("At least 40 peer observations are required for model v2")
    for feature in features:
        sample[feature] = pd.to_numeric(sample[feature], errors="coerce").replace([np.inf, -np.inf], np.nan)
        median = sample[feature].median()
        sample[feature] = sample[feature].fillna(0 if pd.isna(median) else median)
    folds = sample["bond_code"].map(lambda code: zlib.crc32(code.encode()) % 5).to_numpy()
    target = sample["premium_actual_pct"].to_numpy(float)
    predicted = np.full(len(sample), np.nan)
    for fold in range(5):
        test, train = folds == fold, folds != fold
        if not test.any() or train.sum() < 30:
            raise RuntimeError("Insufficient observations in an out-of-fold model partition")
        transformer = ColumnTransformer([
            ("conversion_value_curve", SplineTransformer(n_knots=5, degree=2,
                knots="quantile", include_bias=False, extrapolation="linear"), ["conversion_value"]),
            ("peer_attributes", StandardScaler(), features[1:]),
        ], remainder="drop")
        model = Pipeline([("features", transformer), ("ridge", Ridge(alpha=20))])
        lower, upper = np.quantile(target[train], [0.02, 0.98])
        model.fit(sample.loc[train, features], np.clip(target[train], lower, upper))
        predicted[test] = model.predict(sample.loc[test, features])
    sample["fair_premium_pct"] = predicted
    sample["premium_deviation_pct"] = target - predicted
    if not np.isfinite(predicted).all():
        raise RuntimeError("Non-finite model predictions")
    return sample


def build_payloads(day, sample, expected, existing):
    as_of = day.strftime("%Y-%m-%d")
    snapshot_id = hashlib.sha256(sample[["bond_code", "close", "premium_deviation_pct"]]
                                 .to_csv(index=False, float_format="%.8f").encode()).hexdigest()[:16]
    screened = sample.loc[
        sample["close"].between(90, 120) & sample["ytm_pct"].gt(0)
        & sample["ytm_pct"].le(10) & sample["underlying_close"].ge(1.5)
        & ~sample["notice_active"]
    ].sort_values(["premium_deviation_pct", "bond_code"]).head(10)
    strategy = dict(existing)
    strategy.update({
        "asOf": as_of, "snapshotId": snapshot_id, "modelVersion": 2,
        "dataSource": "东方财富转债历史估值及债券条款",
        "updateSchedule": "交易日收盘后自动更新；每月首个交易日按前收信号调仓",
        "marketCoverage": {"used": len(sample), "quotedActive": expected},
        "bonds": [{
            "code": row.bond_code, "name": row["name"], "price": float(row.close),
            "deviation": float(row.premium_deviation_pct), "premium": float(row.premium_actual_pct),
            "fairPremium": float(row.fair_premium_pct), "ytm": float(row.ytm_pct),
            "underlying": float(row.underlying_close),
        } for _, row in screened.iterrows()],
    })
    strategy["rules"] = dict(strategy["rules"], position="最多10只；新买标的均分可用现金，已有持仓保持数量")
    points = [{"code": row.bond_code, "x": round(float(row.conversion_value), 2),
               "actual": round(float(row.premium_actual_pct), 2),
               "fair": round(float(row.fair_premium_pct), 2),
               "deviation": round(float(row.premium_deviation_pct), 2)}
              for row in sample.sort_values("conversion_value").itertuples(index=False)]
    grouped = sample.assign(bin_start=(sample["conversion_value"] // 20 * 20).astype(int)).groupby("bin_start").agg(
        fair=("fair_premium_pct", "median"), count=("fair_premium_pct", "size")).reset_index()
    curve = [{"x": int(row.bin_start + 10), "range": f"{row.bin_start}–{row.bin_start + 20}",
              "fair": round(float(row.fair), 2), "count": int(row.count)}
             for row in grouped.itertuples(index=False) if row.count >= 3]
    if len(curve) < 3:
        raise RuntimeError("Insufficient fitted curve bins")
    fit = {
        "asOf": as_of, "snapshotId": snapshot_id, "totalActive": len(points),
        "quotedActive": expected,
        "xMax": int(math.ceil(sample["conversion_value"].max() / 100) * 100),
        "yMinPct": -10, "yMaxPct": 200, "binWidth": 20, "minimumBinCount": 3,
        "clippedActualCount": int(sample["premium_actual_pct"].gt(200).sum()),
        "belowRangeCount": int(sample["premium_actual_pct"].lt(-10).sum()),
        "points": points, "curve": curve,
        "method": "Quoted active bonds; includes redemption notices in the valuation universe. Model v2 uses deterministic bond-code out-of-fold estimates, grouped by conversion value in 20-yuan bins.",
    }
    # Avoid a new timestamp-only commit on market holidays or unchanged runs.
    stable_old = {k: v for k, v in existing.items() if k != "updatedAt"}
    stable_new = {k: v for k, v in strategy.items() if k != "updatedAt"}
    strategy["updatedAt"] = (existing.get("updatedAt") if stable_old == stable_new
                             else datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"))
    return strategy, fit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--offline-source", type=Path, help="Use a saved source response for reproducibility audits")
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    data_dir = args.root / "data"
    existing = json.loads((data_dir / "strategy.json").read_text(encoding="utf-8"))
    source = (json.loads(args.offline_source.read_text(encoding="utf-8")) if args.offline_source
              else fetch_source(today))
    day, frame, expected = prepare_frame(source, today)
    if day < pd.Timestamp(existing["asOf"]):
        raise RuntimeError("Source snapshot moved backwards; keeping the last verified data")
    sample = fit_model(frame)
    strategy, fit = build_payloads(day, sample, expected, existing)
    payloads = {"strategy.json": strategy, "fit_curve.json": fit}
    # Serialize and validate every output before replacing any deployed snapshot.
    staged = {}
    for name, payload in payloads.items():
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          indent=2 if name == "strategy.json" else None)
        json.loads(text)
        staged[name] = (text + "\n").encode("utf-8")
    # Actions commits all files together and deploys only after this succeeds.
    for name, content in staged.items():
        (data_dir / name).write_bytes(content)
    print(f"Saved snapshot {strategy['asOf']} ({strategy['snapshotId']}): {len(strategy['bonds'])} candidates; {fit['totalActive']} market points", flush=True)
    print(f"Historical backtest remains explicitly dated through {strategy['backtest']['periodEnd']}", flush=True)


if __name__ == "__main__":
    main()
