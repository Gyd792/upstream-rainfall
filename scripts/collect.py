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


def extract_js_values(text: str, names: tuple[str, ...]) -> list[Any]:
    """Extract JSON-compatible object/array values assigned to known JS globals."""
    found: list[Any] = []
    name_pattern = "|".join(re.escape(name) for name in names)
    assignment = re.compile(
        r"(?:(?:var|let|const)\s+)?(?:" + name_pattern + r")\s*=\s*",
        re.IGNORECASE,
    )
    for match in assignment.finditer(text):
        start = match.end()
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text) or text[start] not in "[{":
            continue
        opening = text[start]
        closing = "}" if opening == "{" else "]"
        stack = []
        quote = None
        escaped = False
        end = None
        for index in range(start, len(text)):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in ("'", '"'):
                quote = char
            elif char in "[{":
                stack.append("}" if char == "{" else "]")
            elif char in "]}":
                if not stack or stack[-1] != char:
                    break
                stack.pop()
                if not stack:
                    end = index + 1
                    break
        if end is None:
            continue
        raw = text[start:end]
        try:
            found.append(json.loads(raw))
        except json.JSONDecodeError:
            # Never eval page JavaScript. Only accept strict JSON payloads.
            continue
    return found


def find_observe24h(value: Any) -> dict[str, Any] | None:
    """Find the 24-hour observed series in either page-global data shape."""
    if isinstance(value, dict):
        # Current and legacy names used by China Weather page scripts.
        for key in ("observe24h", "observe24h_data", "observe24hData"):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                # China Weather page commonly nests the 24-hour chart under
                # observe24h_data.od, where od0 and od2 actually live.
                inner = candidate.get("od")
                if isinstance(inner, dict) and isinstance(inner.get("od2"), list):
                    return inner
                if isinstance(candidate.get("od2"), list):
                    return candidate
                nested = find_observe24h(candidate)
                if nested is not None:
                    return nested
            if isinstance(candidate, list):
                nested = find_observe24h(candidate)
                if nested is not None:
                    return nested
        if isinstance(value.get("od2"), list):
            return value
        # Some versions expose the hourly point list directly.
        for key in ("data", "list", "hours", "hourly", "rows"):
            candidate = value.get(key)
            if isinstance(candidate, list) and candidate and all(
                isinstance(item, dict) and ("od21" in item or "time" in item)
                and ("od26" in item or "rain" in item or "precip" in item)
                for item in candidate
            ):
                return {"od0": value.get("od0") or value.get("time"), "od2": candidate}
        for child in value.values():
            result = find_observe24h(child)
            if result is not None:
                return result
    elif isinstance(value, list):
        if value and all(
            isinstance(item, dict) and ("od21" in item or "time" in item)
            and ("od26" in item or "rain" in item or "precip" in item)
            for item in value
        ):
            return {"od2": value}
        for child in value:
            result = find_observe24h(child)
            if result is not None:
                return result
    return None


def fetch_page_text(url: str) -> str:
    request = urllib.request.Request(url, headers={
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/javascript,text/javascript,*/*;q=0.8",
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_rolling_24h(name: str, code: str, now: datetime) -> list[dict[str, Any]]:
    """Read the city's observed 24-hour chart and its page-loaded data scripts.

    The chart may be initialized by a page global (observe24h_data) or a
    separate JavaScript resource. Inspect both the standard and one-day page.
    Accept only a verified 24-hour sequence of observed rainfall values.
    """
    page_urls = [
        f"https://www.weather.com.cn/weather/{code}.shtml",
        f"https://www.weather.com.cn/weather1d/{code}.shtml",
    ]
    pages: list[tuple[str, str]] = []
    script_urls: list[str] = []
    for page_url in page_urls:
        try:
            page = fetch_page_text(page_url + f"?_={int(time.time() * 1000)}")
            pages.append((page_url, page))
            for src in re.findall(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", page, re.I):
                absolute = urllib.parse.urljoin(page_url, src)
                host = urllib.parse.urlparse(absolute).hostname or ""
                if host.endswith(("weather.com.cn", "tq121.com.cn")) and absolute not in script_urls:
                    script_urls.append(absolute)
        except Exception as exc:
            print(f"CHART PAGE WARNING {name} {page_url}: {exc}")

    # First inspect inline globals. The 24h series has appeared under multiple
    # names in different China Weather page builds.
    candidate_values: list[Any] = []
    for _, page in pages:
        candidate_values.extend(extract_js_values(
            page, ("observe24h", "observe24h_data", "observe24hData", "observe24hDataJson")
        ))

    # Inline observe24h_data is the normal China Weather page path. Resolve
    # it first; only request external scripts when the HTML did not contain
    # a usable series, keeping the scheduled 16-city job bounded.
    observe = None
    for value in candidate_values:
        observe = find_observe24h(value)
        if observe is not None:
            break

    relevant_scripts = [
        url for url in script_urls
        if re.search(r"(observe|weather|hour|chart|sk_2d)", url, re.I)
    ][:4]
    if observe is None:
        for script_url in relevant_scripts:
            try:
                script_text = fetch_page_text(script_url)
                candidate_values.extend(extract_js_values(
                    script_text, ("observe24h", "observe24h_data", "observe24hData", "observe24hDataJson")
                ))
                for value in candidate_values:
                    observe = find_observe24h(value)
                    if observe is not None:
                        break
                if observe is not None:
                    break
            except Exception as exc:
                print(f"CHART SCRIPT WARNING {name} {script_url}: {exc}")

    if observe is None:
        # Emit useful, bounded diagnostics so the next source change is
        # evidence-based rather than guessing another field name.
        page_text = "\n".join(page for _, page in pages)
        globals_seen = sorted(set(re.findall(
            r"(?:var|let|const)\s+([A-Za-z_$][\w$]*(?:observe|hour|rain)[\w$]*)\s*=",
            page_text, re.I
        )))
        script_names = [urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] for url in relevant_scripts]
        raise ValueError(
            "24h chart series not found; page_globals=" + ",".join(globals_seen[:20]) +
            "; chart_scripts=" + ",".join(script_names[:12]) +
            "; pages=" + str(len(pages))
        )

    # China Weather's od0 is the chart date (YYYYMMDD), not a timestamp.
    # Its od2 array may be newest-first and can include one null boundary row.
    anchor_raw = str(observe.get("od0", "")).strip()
    chart_date = None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d%H%M"):
        try:
            chart_date = datetime.strptime(anchor_raw[:len(datetime.now().strftime(fmt))], fmt).date()
            break
        except ValueError:
            continue
    if chart_date is None:
        raise ValueError(f"chart date od0 is missing or invalid: {anchor_raw!r}")

    points = observe.get("od2")
    if not isinstance(points, list):
        raise ValueError("24h chart od2 is not a list")

    # The chart payload can contain a sentinel row with od22 == "null".
    # Exclude only that explicitly invalid row; never turn it into 0 mm.
    points = [
        point for point in points
        if not (isinstance(point, dict) and str(point.get("od22", "")).strip().lower() == "null")
    ]
    if len(points) not in (24, 25):
        raise ValueError(f"24h chart has invalid usable point count: {len(points)}")

    hour_values: list[int] = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError(f"invalid hourly point at index {index}")
        raw_hour = str(point.get("od21", point.get("time", ""))).strip()
        match = re.search(r"(?:^|\\s)(\\d{1,2})(?::\\d{2})?$", raw_hour)
        if not match:
            raise ValueError(f"invalid hourly label at index {index}: {raw_hour!r}")
        hour_value = int(match.group(1))
        if not 0 <= hour_value <= 23:
            raise ValueError(f"hour out of range at index {index}: {raw_hour!r}")
        hour_values.append(hour_value)

    ascending = all((hour_values[i] + 1) % 24 == hour_values[i + 1] for i in range(len(hour_values) - 1))
    descending = all((hour_values[i] - 1) % 24 == hour_values[i + 1] for i in range(len(hour_values) - 1))
    if not (ascending or descending):
        raise ValueError("chart hour labels are not a continuous hourly sequence")

    # Normalize to oldest -> newest. Some page builds serialize newest first.
    if descending:
        points.reverse()
        hour_values.reverse()

    # 25 ticks describe 24 hourly intervals; discard only the oldest
    # boundary sample so the retained data covers the latest 24 hours.
    if len(points) == 25:
        points = points[1:]
        hour_values = hour_values[1:]

    if len(set(hour_values)) != 24:
        raise ValueError("chart does not contain 24 unique hourly observations")

    # od0 identifies the current chart date. Hours later than the current
    # Beijing hour belong to the previous calendar day in a rolling window.
    latest_chart_hour = hour_values[-1]
    result = []
    for index, point in enumerate(points):
        hour_value = hour_values[index]
        point_date = chart_date - timedelta(days=1) if hour_value > latest_chart_hour else chart_date
        stamp = datetime.combine(point_date, datetime.min.time(), tzinfo=BEIJING).replace(hour=hour_value)
        raw_rain = point.get("od26", point.get("rain", point.get("precip")))
        rain_value = as_mm(raw_rain)
        result.append({
            "city": name, "code": code,
            "observed_at": stamp.isoformat(timespec="minutes"),
            "date": stamp.date().isoformat(), "hour": stamp.hour,
            "observation_date": stamp.date().isoformat(),
            "observation_hour": stamp.hour,
            "collected_at": now.isoformat(timespec="minutes"),
            "collection_date": now.date().isoformat(), "collection_hour": now.hour,
            "rain_1h_mm": rain_value, "rain_24h_mm": None,
            "source": "China Weather rolling 24-hour hourly observation chart",
            "source_url": next((url for url, _ in pages if url.endswith(f"/{code}.shtml")), page_urls[0]),
            "field_1h": "observe24h_data.od.od2[].od26",
            "field_24h": None,
            "chart_snapshot_at": now.isoformat(timespec="minutes"),
            "backfilled": True,
            "status": "ok" if rain_value is not None else "missing_in_source",
        })
    return result

def observation_datetime(item: dict[str, Any]) -> datetime | None:
    """Return the actual observed timestamp, never the later collection time."""
    raw = item.get("observed_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed.astimezone(BEIJING)
        except ValueError:
            pass
    date_value = item.get("observation_date") or item.get("date")
    hour_value = item.get("observation_hour", item.get("hour"))
    if date_value is not None and hour_value is not None:
        try:
            return datetime.strptime(
                f"{date_value} {int(hour_value):02d}", "%Y-%m-%d %H"
            ).replace(tzinfo=BEIJING)
        except (TypeError, ValueError):
            return None
    return None


def observation_hour_key(item: dict[str, Any]) -> tuple[str, str, int] | None:
    """Canonical city/date/hour key based on the observation time."""
    stamp = observation_datetime(item)
    if stamp is None:
        return None
    return (str(item.get("city", "")), stamp.date().isoformat(), stamp.hour)


def observation_rank(item: dict[str, Any]) -> tuple[int, float, float]:
    """Prefer valid measurements, then newer observed time, then newer collection."""
    stamp = observation_datetime(item)
    observed_epoch = stamp.timestamp() if stamp else 0.0
    try:
        collected = datetime.fromisoformat(
            str(item.get("collected_at") or "").replace("Z", "+00:00")
        )
        if collected.tzinfo is None:
            collected = collected.replace(tzinfo=BEIJING)
        collected_epoch = collected.timestamp()
    except ValueError:
        collected_epoch = 0.0
    return (1 if item.get("rain_1h_mm") is not None else 0,
            observed_epoch, collected_epoch)


def upsert_observation(observations: list[dict[str, Any]],
                       incoming: dict[str, Any]) -> str:
    """Insert or update a city-hour only with a valid, newer real observation."""
    key = observation_hour_key(incoming)
    if key is None:
        raise ValueError("incoming observation has no valid observed_at timestamp")
    incoming_stamp = observation_datetime(incoming)
    incoming["observation_date"] = incoming_stamp.date().isoformat()
    incoming["observation_hour"] = incoming_stamp.hour
    incoming["date"] = incoming_stamp.date().isoformat()
    incoming["hour"] = incoming_stamp.hour
    matches = [(i, old) for i, old in enumerate(observations)
               if observation_hour_key(old) == key]
    if not matches:
        observations.append(incoming)
        return "inserted"

    # Keep a single canonical record for this city-hour. Existing valid values
    # are protected from null responses; a later actual observation supersedes
    # an earlier value for the same hour.
    best_index, best = max(matches, key=lambda pair: observation_rank(pair[1]))
    incoming_valid = incoming.get("rain_1h_mm") is not None
    best_valid = best.get("rain_1h_mm") is not None
    incoming_rank = observation_rank(incoming)
    best_rank = observation_rank(best)
    if incoming_valid and (not best_valid or incoming_rank[1:] >= best_rank[1:]):
        observations[best_index] = incoming
        action = "updated"
    else:
        action = "kept_existing"
    # Remove duplicate legacy rows for this same city-hour.
    for index, _ in reversed(matches):
        if index != best_index:
            del observations[index]
            if index < best_index:
                best_index -= 1
    return action


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
    observed_hour = observed_at.replace(minute=0, second=0, microsecond=0)
    return {
        "city": name, "code": code,
        "observed_at": observed_at.isoformat(timespec="minutes"),
        "date": observed_hour.date().isoformat(), "hour": observed_hour.hour,
        "collected_at": now.isoformat(timespec="minutes"),
        "collection_date": now.date().isoformat(), "collection_hour": now.hour,
        "observation_date": observed_hour.date().isoformat(),
        "observation_hour": observed_hour.hour,
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
            action = upsert_observation(observations, observation)
            success += 1
            print(f"OK {name}: {observation['observed_at']} 1h={observation['rain_1h_mm']}mm 24h={observation['rain_24h_mm']}mm")
        except Exception as exc:
            failures.append({"city": name, "code": code, "error": str(exc), "checked_at": now.isoformat()})
            print(f"ERROR {name} ({code}): {exc}")
        time.sleep(0.25)

    # Backfill from the same rolling 24-hour hourly chart shown on each
    # city weather page. Every run reads the latest 24-hour snapshot and fills
    # only missing city-hours; existing valid observations are never overwritten.
    chart_attempted = 0
    chart_filled = 0
    chart_unavailable = 0
    chart_errors = []
    existing_keys = {
        (o.get("city"),
         o.get("observation_date") or o.get("collection_date") or o.get("date"),
         int(o.get("observation_hour", o.get("collection_hour", o.get("hour", -1)))))
        for o in observations
    }
    for name, code in CITIES:
        chart_attempted += 1
        try:
            chart_points = fetch_rolling_24h(name, code, now)
            city_added = 0
            for point in chart_points:
                key = observation_hour_key(point)
                old = next((o for o in observations if observation_hour_key(o) == key), None)
                if old is not None and old.get("rain_1h_mm") is not None:
                    continue
                if point.get("rain_1h_mm") is None:
                    chart_unavailable += 1
                    continue
                action = upsert_observation(observations, point)
                if action in ("inserted", "updated"):
                    chart_filled += 1
                    city_added += 1
            print(f"CHART BACKFILL {name}: filled {city_added} missing hours from rolling 24h chart")
        except Exception as exc:
            chart_errors.append({"city": name, "code": code, "error": str(exc)})
            print(f"CHART BACKFILL ERROR {name}: {exc}")
        time.sleep(0.35)

    unique: dict[tuple[str, str, int], dict[str, Any]] = {}
    for item in observations:
        key = observation_hour_key(item)
        if key is None:
            continue
        previous = unique.get(key)
        if previous is None or observation_rank(item) > observation_rank(previous):
            unique[key] = item
    data["hourly_observations"] = sorted(
        unique.values(), key=lambda x: (x.get("observed_at", ""), x.get("city", "")))
    update_rollups(data, now)
    data["missing_hours_report"] = build_missing_report(data, now)
    data["backfill_report"] = {
        "checked_at": now.isoformat(timespec="minutes"),
        "mode": "rolling_24h_chart_backfill",
        "source": "China Weather city page observe24h_data.od.od2[].od26",
        "attempted_cities": chart_attempted,
        "filled_city_hours": chart_filled,
        "source_missing_points": chart_unavailable,
        "failed_cities": chart_errors,
        "window_hours": 24,
        "policy": "fill missing city-hours only from verified rolling 24-hour observed chart values; live feed rechecks every run and newer valid observed_at values replace earlier values in the same city-hour; never copy adjacent-hour values",
    }
    data["note"] = (
        "每次采集均重新检查当前实况，并按真实观测时间归入北京时间小时；同一小时取得更新的真实观测值时更新该小时记录。"
        "最近24小时图表仅在成功取得并验证逐小时实测值时补齐缺测；图表解析失败或源数据缺失时保留空值，不使用预测值、不复制相邻小时数据。"
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
            "history_policy": "group by actual observed_at Beijing hour; every scheduled run rechecks the current hour; newer valid observation timestamps replace older values within the same city-hour",
            "source": "https://d1.weather.com.cn/sk_2d/{area_code}.html",
            "historical_backfill": "rolling_24h_chart_backfill",
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
