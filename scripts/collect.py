#!/usr/bin/env python3
"""Collect hourly observed precipitation from China Weather public observation feed.

The collector is deliberately fail-closed: if a city response or precipitation
fields cannot be parsed, it records a diagnostic and leaves that value missing.
It never substitutes forecast precipitation or zero for a failed request.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "rainfall.json"
BEIJING = timezone(timedelta(hours=8))
BASE_URL = "https://d1.weather.com.cn/sk_2d/{code}.html"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; YunpengRainfallMonitor/1.0)",
    "Referer": "https://www.weather.com.cn/",
    "Accept": "text/javascript, application/javascript, */*;q=0.8",
}

CITIES = [
    ("曲靖", "101290401"), ("陆良", "101290403"),
    ("宜良", "101290106"), ("石林", "101290107"),
    ("澄江", "101290702"), ("江川", "101290703"),
    ("玉溪", "101290701"), ("峨山", "101290708"),
    ("通海", "101290704"), ("华宁", "101290705"),
    ("建水", "101290303"), ("蒙自", "101290309"),
    ("石屏", "101290302"), ("开远", "101290307"),
    ("弥勒", "101290304"), ("泸西", "101290311"),
]

RAIN_1H_KEYS = ("rain1h", "rain_1h", "precip1h", "precip_1h",
                "precipitation1h", "rainfall1h", "rain")
RAIN_24H_KEYS = ("rain24h", "rain_24h", "precip24h", "precip_24h",
                 "precipitation24h", "rainfall24h", "rain24")
TIME_KEYS = ("time", "observeTime", "obstime", "dateTime", "datetime", "publishTime")


def load_data() -> dict[str, Any]:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"title": "云鹏电站上游流域降雨量数据", "unit": "mm",
            "cities": [name for name, _ in CITIES], "records": [],
            "hourly_observations": []}


def extract_object(text: str) -> dict[str, Any]:
    match = re.search(r"\bdataSK\s*=\s*", text)
    if not match:
        raise ValueError("response does not contain dataSK object")
    start = text.find("{", match.end())
    if start < 0:
        raise ValueError("dataSK object start not found")
    depth = 0
    quote = None
    escaped = False
    end = None
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise ValueError("incomplete dataSK object")
    raw = text[start:end]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # The public endpoint normally emits JSON. Do not eval JavaScript.
        raise ValueError("dataSK payload is not strict JSON")


def pick(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[Any, str | None]:
    lower = {str(k).lower(): k for k in data}
    for candidate in keys:
        original = lower.get(candidate.lower())
        if original is not None:
            return data[original], str(original)
    return None, None


def as_mm(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        raw = str(value).strip().replace("毫米", "").replace("mm", "").replace("MM", "")
        if raw in ("", "-", "--", "无", "暂无", "None", "null"):
            return None
        try:
            number = float(raw)
        except ValueError:
            return None
    if number < 0 or number > 1000:
        return None
    return round(number, 1)


def parse_observation_time(data: dict[str, Any], now: datetime) -> datetime:
    value, _ = pick(data, TIME_KEYS)
    if value is None:
        raise ValueError("observation timestamp field not found")
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y%m%d%H%M", "%Y%m%d%H", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=BEIJING)
        except ValueError:
            pass
    # Some feeds return HH:mm only; accept it only if it is close to this run.
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if match:
        parsed = now.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                             second=0, microsecond=0)
        if abs((now - parsed).total_seconds()) <= 3 * 3600:
            return parsed
    raise ValueError(f"unrecognized observation timestamp: {raw}")


def fetch_city(name: str, code: str, now: datetime) -> dict[str, Any]:
    url = BASE_URL.format(code=code) + f"?_={int(time.time() * 1000)}"
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")
    data = extract_object(body)
    rain1, rain1_key = pick(data, RAIN_1H_KEYS)
    rain24, rain24_key = pick(data, RAIN_24H_KEYS)
    observed_at = parse_observation_time(data, now)
    if rain1_key is None:
        raise ValueError("1-hour precipitation field not found; returned keys=" +
                         ",".join(map(str, data.keys())))
    return {
        "city": name, "code": code,
        "observed_at": observed_at.isoformat(timespec="minutes"),
        "date": observed_at.date().isoformat(), "hour": observed_at.hour,
        "rain_1h_mm": as_mm(rain1), "rain_24h_mm": as_mm(rain24),
        "source": "China Weather public observation endpoint",
        "source_url": BASE_URL.format(code=code),
        "field_1h": rain1_key, "field_24h": rain24_key,
        "status": "ok",
    }


def sum_hours(by_hour: dict[int, float | None], hours: list[int]) -> float | None:
    values = [by_hour.get(hour) for hour in hours]
    if any(value is None for value in values):
        return None
    return round(sum(values), 1)


def build_daily_record(date_str: str, city_index: int,
                       obs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    prev = (day - timedelta(days=1)).isoformat()
    city_name = CITIES[city_index][0]
    selected = [o for o in obs if o.get("city") == city_name]
    by_stamp: dict[tuple[str, int], float | None] = {}
    by_date_hour: dict[tuple[str, int], float | None] = {}
    for item in selected:
        key = (item["date"], int(item["hour"]))
        # Prefer the most recent duplicate observation for a clock hour.
        by_date_hour[key] = item.get("rain_1h_mm")
    morning_hours = [(prev, 21), (prev, 22), (prev, 23)] + [
        (date_str, h) for h in range(0, 8)]
    evening_hours = [(date_str, h) for h in range(8, 21)]
    morning_values = [by_date_hour.get(key) for key in morning_hours]
    evening_values = [by_date_hour.get(key) for key in evening_hours]
    morning = None if any(v is None for v in morning_values) else round(sum(morning_values), 1)
    direct_evening = None if any(v is None for v in evening_values) else round(sum(evening_values), 1)

    latest_20 = [o for o in selected
                 if o.get("date") == date_str and int(o.get("hour", -1)) == 20
                 and o.get("rain_24h_mm") is not None]
    total_24h = latest_20[-1]["rain_24h_mm"] if latest_20 else None
    corrected = None
    status = "missing"
    difference = None
    if morning is not None and total_24h is not None and total_24h >= morning:
        corrected = round(total_24h - morning, 1)
        if direct_evening is None:
            status = "derived_24h_missing_hourly"
        else:
            difference = round(direct_evening - corrected, 1)
            status = "matched" if abs(difference) <= 0.1 else "corrected_by_24h"
    elif direct_evening is not None:
        corrected = direct_evening
        status = "direct_only_no_24h_validation"

    return (
        {"city": city_name, "morning_mm": morning,
         "coverage_morning": sum(v is not None for v in morning_values), "expected_morning": 11},
        {"city": city_name, "evening_direct_mm": direct_evening,
         "evening_mm": corrected, "total_24h_mm": total_24h,
         "validation_status": status, "difference_mm": difference,
         "coverage_evening": sum(v is not None for v in evening_values), "expected_evening": 13}
    )


def update_rollups(data: dict[str, Any], now: datetime) -> None:
    date_str = now.date().isoformat()
    records = data.setdefault("records", [])
    # Morning report is finalized by the 07:40 Beijing run.
    if now.hour == 7:
        morning_values, _ = [], []
        for i in range(len(CITIES)):
            morning, _evening = build_daily_record(date_str, i, data.get("hourly_observations", []))
            morning_values.append(morning["morning_mm"])
        existing = next((r for r in records if r.get("date") == date_str and r.get("period") == "morning"), None)
        row = {"date": date_str, "period": "morning", "values": morning_values,
               "source": "hourly observed precipitation", "updated_at": now.isoformat(timespec="minutes")}
        if existing:
            records[records.index(existing)] = row
        else:
            records.append(row)

    # Evening report is finalized by the 20:40 Beijing run.
    if now.hour == 20:
        evening_values, total_values, statuses = [], [], []
        for i in range(len(CITIES)):
            _morning, evening = build_daily_record(date_str, i, data.get("hourly_observations", []))
            evening_values.append(evening["evening_mm"])
            total_values.append(evening["total_24h_mm"])
            statuses.append(evening["validation_status"])
        existing = next((r for r in records if r.get("date") == date_str and r.get("period") == "evening"), None)
        row = {"date": date_str, "period": "evening", "values": evening_values,
               "total_24h_mm": total_values, "validation_status": statuses,
               "source": "hourly observed precipitation; checked against 24-hour total",
               "updated_at": now.isoformat(timespec="minutes")}
        if existing:
            records[records.index(existing)] = row
        else:
            records.append(row)
    records.sort(key=lambda r: (r.get("date", ""), r.get("period", "")))
    data["records"] = records


def main() -> int:
    now = datetime.now(BEIJING).replace(second=0, microsecond=0)
    data = load_data()
    data.setdefault("title", "云鹏电站上游流域降雨量数据")
    data.setdefault("cities", [name for name, _ in CITIES])
    data.setdefault("regions", [
        {"name": "曲靖地区", "cities": ["曲靖", "陆良"]},
        {"name": "昆明地区", "cities": ["宜良", "石林"]},
        {"name": "玉溪地区", "cities": ["澄江", "江川", "玉溪", "峨山", "通海", "华宁"]},
        {"name": "红河地区", "cities": ["建水", "蒙自", "石屏", "开远", "弥勒", "泸西"]},
    ])
    observations = data.setdefault("hourly_observations", [])
    success = 0
    failures = []
    for index, (name, code) in enumerate(CITIES):
        try:
            observation = fetch_city(name, code, now)
            # Replace same city + observation timestamp, preserving a single record.
            observations = [o for o in observations
                            if not (o.get("city") == name and o.get("observed_at") == observation["observed_at"])]
            observations.append(observation)
            success += 1
            print(f"OK {name}: {observation['observed_at']} 1h={observation['rain_1h_mm']}mm 24h={observation['rain_24h_mm']}mm")
        except Exception as exc:
            failures.append({"city": name, "code": code, "error": str(exc), "checked_at": now.isoformat()})
            print(f"ERROR {name} ({code}): {exc}")
        time.sleep(0.5)

    # Keep a rolling 48-hour raw series; finalized daily summaries remain permanent.
    cutoff = now - timedelta(hours=48)
    kept = []
    for item in observations:
        try:
            stamp = datetime.fromisoformat(item["observed_at"])
            if stamp >= cutoff:
                kept.append(item)
        except Exception:
            continue
    data["hourly_observations"] = sorted(kept, key=lambda x: (x.get("observed_at", ""), x.get("city", "")))
    update_rollups(data, now)
    data["updated_at"] = now.isoformat(timespec="minutes")
    data["last_collection"] = {
        "checked_at": now.isoformat(timespec="minutes"),
        "success_count": success, "expected_count": len(CITIES),
        "failures": failures,
        "source": "https://d1.weather.com.cn/sk_2d/{area_code}.html",
    }
    data["note"] = "空值表示未取得完整且可核实的实测数据，不代表0毫米；逐小时实况缺测不会补零。"
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Collection complete: {success}/{len(CITIES)} cities; failures={len(failures)}")
    return 0 if success > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
