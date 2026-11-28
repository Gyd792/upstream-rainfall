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
RAIN_24H_KEYS = ("rain24", "rain24h", "rain_24h", "precip24h", "precip_24h",
                 "precipitation24h", "rainfall24h")
TIME_KEYS = ("observeTime", "obstime", "dateTime", "datetime", "publishTime", "time")
DATE_KEYS = ("date", "observeDate", "observationDate")


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
    date_value, _ = pick(data, DATE_KEYS)
    if value is None:
        raise ValueError("observation timestamp field not found")
    raw = str(value).strip()
    date_raw = str(date_value).strip() if date_value is not None else ""
    # China Weather feed date can be YYYYMMDD or "11月26日(星期四)".
    chinese_date = re.search(r"(\d{1,2})月(\d{1,2})日", date_raw)
    date_prefix = None
    if date_raw and re.fullmatch(r"\d{8}", date_raw):
        date_prefix = date_raw
    elif chinese_date:
        month, day = int(chinese_date.group(1)), int(chinese_date.group(2))
        year = now.year
        if month > now.month + 1:
            year -= 1
        date_prefix = f"{year:04d}{month:02d}{day:02d}"
    if date_prefix:
        for fmt in ("%Y%m%d %H:%M", "%Y%m%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(date_prefix + " " + raw, fmt)
                return parsed.replace(tzinfo=BEIJING)
            except ValueError:
                pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y%m%d%H%M", "%Y%m%d%H", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=BEIJING)
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if match:
        parsed = now.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                             second=0, microsecond=0)
        if abs((now - parsed).total_seconds()) <= 3 * 3600:
            return parsed
    raise ValueError(f"unrecognized observation timestamp: {raw}; date={date_raw or 'missing'}")


def fetch_city(name: str, code: str, now: datetime) -> dict[str, Any]:
    url = BASE_URL.format(code=code) + f"?_={int(time.time() * 1000)}"
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=10) as response:
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
        "collected_at": now.isoformat(timespec="minutes"),
        "collection_date": now.date().isoformat(), "collection_hour": now.hour,
        "observation_date": observed_at.date().isoformat(),
        "observation_hour": observed_at.hour,
        "rain_1h_mm": as_mm(rain1), "rain_24h_mm": as_mm(rain24),
        "source": "China Weather public observation endpoint",
        "source_url": BASE_URL.format(code=code),
        "field_1h": rain1_key, "field_24h": rain24_key,
        "status": "ok",
    }


def fetch_city_with_retry(name: str, code: str, now: datetime, attempts: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fetch_city(name, code, now)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                delay = attempt * 2
                print(f"RETRY {name} ({code}) in {delay}s: {exc}")
                time.sleep(delay)
    raise RuntimeError(f"failed after {attempts} attempts: {last_error}")


def build_missing_report(data: dict[str, Any], now: datetime) -> dict[str, Any]:
    end = now.replace(minute=0, second=0, microsecond=0)
    expected = [(end - timedelta(hours=i)).strftime("%Y-%m-%dT%H") for i in range(23, -1, -1)]
    observations = data.get("hourly_observations", [])
    rows = []
    for name, code in CITIES:
        available = set()
        for item in observations:
            if item.get("city") != name or item.get("rain_1h_mm") is None:
                continue
            try:
                if item.get("observation_date") is not None and item.get("observation_hour") is not None:
                    key = "{}T{:02d}".format(item["observation_date"], int(item["observation_hour"]))
                else:
                    stamp = item.get("observed_at") or item.get("collected_at")
                    dt = datetime.fromisoformat(stamp).astimezone(BEIJING)
                    key = dt.strftime("%Y-%m-%dT%H")
                available.add(key)
            except (TypeError, ValueError, KeyError):
                continue
        missing = [hour for hour in expected if hour not in available]
        rows.append({"city": name, "code": code, "coverage": 24-len(missing),
                     "expected": 24, "missing_hours": missing})
    missing_count = sum(len(row["missing_hours"]) for row in rows)
    return {"timezone": "Asia/Shanghai (UTC+08:00)", "window_start": expected[0],
            "window_end": expected[-1], "generated_at": end.isoformat(timespec="minutes"),
            "missing_hour_count": missing_count,
            "covered_hour_count": 16 * 24 - missing_count,
            "expected_hour_count": 16 * 24,
            "cities": rows}


def sum_hours(by_hour: dict[int, float | None], hours: list[int]) -> float | None:
    values = [by_hour.get(hour) for hour in hours]
    if any(value is None for value in values):
        return None
    return round(sum(values), 1)


def build_daily_record(date_str: str, city_index: int,
                       obs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build official period values only when every hour in that period is present."""
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    prev = (day - timedelta(days=1)).isoformat()
    city_name = CITIES[city_index][0]
    selected = [o for o in obs if o.get("city") == city_name]
    by_date_hour: dict[tuple[str, int], float | None] = {}
    for item in selected:
        sample_date = item.get("observation_date") or item.get("collection_date") or item["date"]
        sample_hour = int(item.get("observation_hour", item.get("collection_hour", item["hour"])))
        by_date_hour[(sample_date, sample_hour)] = item.get("rain_1h_mm")

    morning_hours = [(prev, 21), (prev, 22), (prev, 23)] + [
        (date_str, h) for h in range(0, 8)]
    evening_hours = [(date_str, h) for h in range(8, 21)]
    morning_values = [by_date_hour.get(key) for key in morning_hours]
    evening_values = [by_date_hour.get(key) for key in evening_hours]

    morning_complete = all(v is not None for v in morning_values)
    evening_complete = all(v is not None for v in evening_values)
    morning = round(sum(morning_values), 1) if morning_complete else None
    direct_evening = round(sum(evening_values), 1) if evening_complete else None

    latest_20 = [o for o in selected
                 if (o.get("collection_date") or o.get("date")) == date_str
                 and int(o.get("collection_hour", o.get("hour", -1))) == 20
                 and o.get("rain_24h_mm") is not None]
    total_24h = latest_20[-1]["rain_24h_mm"] if latest_20 else None

    # A formal evening value requires all 13 hourly observations.
    # If the 24-hour total and complete morning value are available,
    # retain the established reconciliation rule: evening = 24h - morning.
    evening_value = None
    status = "missing"
    difference = None
    if evening_complete:
        evening_value = direct_evening
        status = "direct_only_no_24h_validation"
        if morning is not None and total_24h is not None and total_24h >= morning:
            reconciled = round(total_24h - morning, 1)
            difference = round(direct_evening - reconciled, 1)
            evening_value = reconciled
            status = "matched" if abs(difference) <= 0.1 else "corrected_by_24h"

    return (
        {"city": city_name, "morning_mm": morning,
         "coverage_morning": sum(v is not None for v in morning_values), "expected_morning": 11},
        {"city": city_name, "evening_direct_mm": direct_evening,
         "evening_mm": evening_value, "total_24h_mm": total_24h,
         "validation_status": status, "difference_mm": difference,
         "coverage_evening": sum(v is not None for v in evening_values), "expected_evening": 13}
    )


def update_rollups(data: dict[str, Any], now: datetime) -> None:
    """Re-evaluate today's summaries on every successful collection.

    This allows a summary to appear as soon as the final missing hourly value
    arrives, including after the nominal 07:40/20:40 target time.
    """
    date_str = now.date().isoformat()
    records = data.setdefault("records", [])
    snapshots = data.setdefault("summary_history", [])

    morning_values = []
    evening_values, total_values, statuses = [], [], []
    for i in range(len(CITIES)):
        morning, evening = build_daily_record(date_str, i, data.get("hourly_observations", []))
        morning_values.append(morning["morning_mm"])
        evening_values.append(evening["evening_mm"])
        total_values.append(evening["total_24h_mm"])
        statuses.append(evening["validation_status"])

    # Do not create a formal row until at least one city's complete period exists.
    if any(value is not None for value in morning_values):
        existing = next((r for r in records if r.get("date") == date_str and r.get("period") == "morning"), None)
        row = {"date": date_str, "period": "morning", "values": morning_values,
               "source": "hourly observed precipitation; only complete 11-hour periods",
               "updated_at": now.isoformat(timespec="minutes")}
        if not existing or existing.get("values") != morning_values:
            if existing:
                records[records.index(existing)] = row
            else:
                records.append(row)
            snapshots.append({**row, "snapshot_at": now.isoformat(timespec="minutes")})

    if any(value is not None for value in evening_values):
        existing = next((r for r in records if r.get("date") == date_str and r.get("period") == "evening"), None)
        row = {"date": date_str, "period": "evening", "values": evening_values,
               "total_24h_mm": total_values, "validation_status": statuses,
               "source": "hourly observed precipitation; only complete 13-hour periods; checked against 24-hour total",
               "updated_at": now.isoformat(timespec="minutes")}
        if (not existing or existing.get("values") != evening_values
                or existing.get("total_24h_mm") != total_values
                or existing.get("validation_status") != statuses):
            if existing:
                records[records.index(existing)] = row
            else:
                records.append(row)
            snapshots.append({**row, "snapshot_at": now.isoformat(timespec="minutes")})

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
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    success = 0
    failures = []
    for name, code in CITIES:
        try:
            observation = fetch_city_with_retry(name, code, now, attempts=3)
            key = (name, observation["observation_date"], observation["observation_hour"])
            old = next((o for o in observations
                        if (o.get("city"),
                            o.get("observation_date") or o.get("collection_date") or o.get("date"),
                            int(o.get("observation_hour", o.get("collection_hour", o.get("hour", -1))))) == key), None)
            if old is None:
                observations.append(observation)
            elif (old.get("rain_1h_mm") != observation.get("rain_1h_mm")
                  or old.get("rain_24h_mm") != observation.get("rain_24h_mm")
                  or old.get("rain_1h_mm") is None):
                observations[observations.index(old)] = observation
            success += 1
            print(f"OK {name}: {observation['observed_at']} 1h={observation['rain_1h_mm']}mm 24h={observation['rain_24h_mm']}mm")
        except Exception as exc:
            failures.append({"city": name, "code": code, "error": str(exc), "checked_at": now.isoformat()})
            print(f"ERROR {name} ({code}): {exc}")
        time.sleep(0.25)

    unique = {}
    for item in sorted(observations, key=lambda x: x.get("collected_at") or x.get("observed_at", "")):
        key = (item.get("city"),
               item.get("observation_date") or item.get("collection_date") or item.get("date"),
               int(item.get("observation_hour", item.get("collection_hour", item.get("hour", -1)))))
        previous = unique.get(key)
        if previous is None or (previous.get("rain_1h_mm") is None and item.get("rain_1h_mm") is not None):
            unique[key] = item
        elif previous.get("rain_1h_mm") == item.get("rain_1h_mm") and previous.get("rain_24h_mm") == item.get("rain_24h_mm"):
            unique[key] = item
    data["hourly_observations"] = sorted(
        unique.values(), key=lambda x: (x.get("collected_at") or x.get("observed_at", ""), x.get("city", "")))
    update_rollups(data, now)
    data["missing_hours_report"] = build_missing_report(data, now)
    data["note"] = (
        "空值表示未取得可核实的实测数据，不代表0毫米；不使用预测或再分析数据填补实测缺测。"
        "每5分钟尝试采集当前实况；历史接口不可用时，过去小时缺测会继续标记，不伪造回填。"
    )

    # Scheduled workflows can be delayed. Persist health once per 15-minute bucket
    # to avoid creating a Git commit on every five-minute poll.
    health_bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    previous_health = data.get("last_collection", {})
    previous_bucket = previous_health.get("health_bucket")
    if previous_bucket != health_bucket.isoformat(timespec="minutes"):
        data["last_collection"] = {
            "checked_at": now.isoformat(timespec="minutes"),
            "health_bucket": health_bucket.isoformat(timespec="minutes"),
            "success_count": success, "expected_count": len(CITIES),
            "failures": failures,
            "history_policy": "one observation timestamp per city and Beijing hour; retry current feed every scheduled run",
            "source": "https://d1.weather.com.cn/sk_2d/{area_code}.html",
            "historical_backfill": "not_available_from_live_endpoint",
        }

    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if after != before:
        data["updated_at"] = now.isoformat(timespec="minutes")
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved rainfall data: {success}/{len(CITIES)} cities; missing hours={data['missing_hours_report']['missing_hour_count']}")
    else:
        print(f"No data changes; latest attempt succeeded for {success}/{len(CITIES)} cities.")
    return 0 if success > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
