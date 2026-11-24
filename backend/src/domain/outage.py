"""夜间停灯：热线熄灯与巡检复亮的整分钟校时、成对和按日拆分。

两路记录分别是热线「熄灯记录」和巡检「复亮记录」。每个夜班为两路各填一段
含首尾的校时范围（以分钟表示），同一路在一个夜班内所有灯组共用同一个整分钟
偏移。把偏移加到该路记录的本机时间上即为校正时间。

校正后同一灯组第 k 次熄灯配第 k 次复亮，要求复亮不早于熄灯，且不晚于下一次
熄灯。只有顺序全部成立的偏移组合才可行。停灯时长按校正后的实际日期拆分，
跨午夜部分分别计入两个实际日期，再乘以在册灯具数得到停灯盏分钟。

一个夜班内的偏移组合彼此独立，所以每天的最高值等于各夜班在该日最高值之和，
最低值同理；每个夜班分别记录取得最高、最低值的偏移。没有可行组合的夜班
（含缺记录、同组记录数不一致等结构问题）单列，不参与每日最高最低值。

校验结果保存在开算快照中。中断后恢复只使用快照里的两路记录、范围和灯数；
已验过并落库的偏移组合直接复用，全部组合就绪后才汇总，保证并发接手时也只
产出一次完整时长结果。
"""

import time
from collections import defaultdict
from datetime import datetime, timedelta

OFF = "熄灯记录"
ON = "复亮记录"
RANGE = "校时范围"
REGISTER = "灯组台账"
HOTLINE = "热线"
INSPECTION = "巡检"
EVENT_TYPES = (OFF, ON)
LANES = (HOTLINE, INSPECTION)
MAX_OFFSET = 1440
MAX_COMBINATIONS = 200_000
BATCH = 512
STAMP = "%Y-%m-%dT%H:%M:%S"


class Incomplete(Exception):
    """偏移组合尚未全部验完，暂不产出时长结果。"""


class _Infeasible(Exception):
    pass


def is_outage_record(record):
    return record.get("record_type") in (OFF, ON, RANGE)


def timestamp(value, label):
    """校验记录方的本机时间，拒绝带时区的输入。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace(" ", "T"))
        except ValueError:
            raise ValueError(f"{label}须使用有效的日期时间，如 2026-11-14T22:12。") from None
    else:
        raise ValueError(f"{label}格式有误，请填写日期时间。")
    if parsed.tzinfo is not None:
        raise ValueError(f"{label}不能带时区，请填写记录方本机时间。")
    return parsed.replace(microsecond=0)


def offset(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}须为整分钟偏移。")
    if not -MAX_OFFSET <= value <= MAX_OFFSET:
        raise ValueError(f"{label}须在 -{MAX_OFFSET} 至 {MAX_OFFSET} 分钟之间。")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空，请填写文本。")
    return value.strip()


def _parse(value, label):
    try:
        return datetime.strptime(value, STAMP)
    except (TypeError, ValueError):
        raise ValueError(f"开算快照中的{label}格式有误。") from None


def build_snapshot(rows):
    """从已校验记录生成可持久化的开算快照：两路记录、校时范围和灯数。"""
    fixtures = {}
    ranges = defaultdict(dict)
    events = defaultdict(lambda: {HOTLINE: defaultdict(list), INSPECTION: defaultdict(list)})
    present = False
    ledger = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"夜间停灯第 {index} 条记录格式有误。")
        kind = row.get("record_type")
        if kind == REGISTER:
            fixtures[_text(row.get("site_id"), f"第 {index} 条台账的灯组编号").upper()] = row["fixture_count"]
        elif kind in (RANGE,) + EVENT_TYPES:
            ledger.append((index, row))
    for index, row in ledger:
        kind = row.get("record_type")
        if kind == RANGE:
            present = True
            night = _text(row.get("night"), f"第 {index} 条校时范围的夜班")
            lane = row.get("lane")
            if lane not in LANES:
                raise ValueError(f"夜班「{night}」的校时路别须为「{HOTLINE}」或「{INSPECTION}」。")
            if lane in ranges[night]:
                raise ValueError(f"夜班「{night}」的{lane}校时范围重复，请合并后重填。")
            low = offset(row.get("offset_min"), f"夜班「{night}」的{lane}校时范围起")
            high = offset(row.get("offset_max"), f"夜班「{night}」的{lane}校时范围止")
            if low > high:
                raise ValueError(f"夜班「{night}」的{lane}校时范围起不能晚于止。")
            ranges[night][lane] = [low, high]
        else:
            present = True
            night = _text(row.get("night"), f"第 {index} 条{kind}的夜班")
            site = _text(row.get("site_id"), f"第 {index} 条{kind}的灯组编号").upper()
            lane = HOTLINE if kind == OFF else INSPECTION
            if site not in fixtures:
                raise ValueError(f"{kind}的灯组「{site}」没有台账行，请补录在册灯具数。")
            moment = timestamp(row.get("recorded_at"), f"{kind}的灯组「{site}」记录时间")
            if moment in events[night][lane][site]:
                raise ValueError(f"夜班「{night}」灯组「{site}」的{kind}时间重复，请合并后重填。")
            events[night][lane][site].append(moment)

    for night in set(events) | set(ranges):
        for lane in LANES:
            if lane not in ranges.get(night, {}):
                raise ValueError(f"夜班「{night}」缺少{lane}的{RANGE}，请为两路各填一段。")
        if night not in events:
            raise ValueError(f"夜班「{night}」只有{RANGE}，没有任何熄灯或复亮记录。")

    total = 0
    for night, lanes in events.items():
        off, on = lanes[HOTLINE], lanes[INSPECTION]
        if not off and not on:
            continue
        if any(len(off[site]) != len(on[site]) for site in set(off) | set(on)):
            continue
        low, high = ranges[night][HOTLINE]
        low_on, high_on = ranges[night][INSPECTION]
        total += (high - low + 1) * (high_on - low_on + 1)
    if total > MAX_COMBINATIONS:
        raise ValueError(f"校时范围的偏移组合超过 {MAX_COMBINATIONS} 个，请缩小范围。")
    if not present:
        return {"fixtures": {site: fixtures[site] for site in sorted(fixtures)}, "ranges": {}, "events": {}}

    return {
        "fixtures": {site: fixtures[site] for site in sorted(fixtures)},
        "ranges": {
            night: {lane: list(ranges[night][lane]) for lane in LANES}
            for night in sorted(ranges)
        },
        "events": {
            night: {
                lane: {
                    site: [moment.strftime(STAMP) for moment in sorted(events[night][lane][site])]
                    for site in sorted(events[night][lane])
                }
                for lane in LANES
            }
            for night in sorted(events)
        },
    }


def has_outage(snapshot):
    return bool(snapshot and (snapshot.get("ranges") or snapshot.get("events")))


def plan(snapshot):
    """列出每个夜班的期望组合数和结构性无解原因。"""
    events = snapshot.get("events", {})
    ranges = snapshot.get("ranges", {})
    nights = []
    expected = 0
    for night in sorted(set(events) | set(ranges)):
        off = events.get(night, {}).get(HOTLINE, {})
        on = events.get(night, {}).get(INSPECTION, {})
        if not off and not on:
            reason = "没有任何熄灯或复亮记录"
        elif not off:
            reason = "缺少熄灯记录"
        elif not on:
            reason = "缺少复亮记录"
        else:
            mismatch = sorted(
                site for site in set(off) | set(on)
                if len(off.get(site, [])) != len(on.get(site, []))
            )
            reason = f"同组熄灯与复亮记录数不一致：{'、'.join(mismatch)}" if mismatch else None
        low, high = ranges[night][HOTLINE]
        low_on, high_on = ranges[night][INSPECTION]
        combos = 0 if reason else (high - low + 1) * (high_on - low_on + 1)
        nights.append({
            "night": night,
            "off_count": sum(len(values) for values in off.values()),
            "on_count": sum(len(values) for values in on.values()),
            "hotline_range": [low, high],
            "inspection_range": [low_on, high_on],
            "combinations": combos,
            "structural": reason,
        })
        expected += combos
    return {"nights": nights, "expected": expected}


def combinations(snapshot, night_plan):
    night = night_plan["night"]
    low, high = snapshot["ranges"][night][HOTLINE]
    low_on, high_on = snapshot["ranges"][night][INSPECTION]
    for hotline in range(low, high + 1):
        for inspection in range(low_on, high_on + 1):
            yield hotline, inspection


def compile_events(snapshot):
    """把快照中的字符串时间预先解析成日期时间，供大量组合复用。"""
    return {
        "fixtures": snapshot.get("fixtures", {}),
        "ranges": snapshot.get("ranges", {}),
        "events": {
            night: {
                lane: {
                    site: [_parse(item, f"{lane}记录时间") for item in times]
                    for site, times in lanes.get(lane, {}).items()
                }
                for lane in LANES
            }
            for night, lanes in snapshot.get("events", {}).items()
        },
    }


def _add_interval(start, end, fixtures, days):
    cursor = start
    while cursor < end:
        following = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time())
        stop = min(end, following)
        days[cursor.date().isoformat()] += int((stop - cursor).total_seconds()) * fixtures
        cursor = stop


def verify(runtime, night_plan, hotline, inspection):
    """校验一个偏移组合；可行时返回按实际日期拆分的停灯秒数。"""
    night = night_plan["night"]
    events = runtime["events"][night]
    fixtures = runtime["fixtures"]
    hotline_shift = timedelta(minutes=hotline)
    inspection_shift = timedelta(minutes=inspection)
    days = defaultdict(int)
    try:
        for site in sorted(set(events[HOTLINE]) | set(events[INSPECTION])):
            offs = [item + hotline_shift for item in events[HOTLINE].get(site, [])]
            ons = [item + inspection_shift for item in events[INSPECTION].get(site, [])]
            if len(offs) != len(ons):
                raise _Infeasible("同组熄灯与复亮记录数不一致")
            for index, (off, on) in enumerate(zip(offs, ons)):
                if on < off:
                    raise _Infeasible(f"灯组 {site} 复亮早于熄灯")
                if index + 1 < len(offs) and ons[index] > offs[index + 1]:
                    raise _Infeasible(f"灯组 {site} 的复亮晚于下一次熄灯")
                _add_interval(off, on, fixtures[site], days)
    except _Infeasible as error:
        return {"feasible": False, "reason": str(error), "days": {}}
    return {"feasible": True, "reason": None, "days": dict(sorted(days.items()))}


def _minutes(seconds):
    if seconds % 60 == 0:
        return seconds // 60
    return round(seconds / 60, 6)


def _entry_text(entry):
    return f"{entry['night']} 热线{entry['hotline']:+d} / 巡检{entry['inspection']:+d}"


def aggregate(snapshot, night_plans, checks):
    """仅在全部组合都已验证后汇总；checks 的键为 (夜班, 热线偏移, 巡检偏移)。"""
    expected = {}
    for night_plan in night_plans:
        if night_plan["structural"]:
            continue
        for hotline, inspection in combinations(snapshot, night_plan):
            expected[(night_plan["night"], hotline, inspection)] = night_plan
    missing = [key for key in expected if key not in checks]
    if missing:
        return {"complete": False, "pending": len(missing), "total": len(expected)}

    per_night = {}
    feasible_total = 0
    for key in expected:
        night = key[0]
        bucket = per_night.setdefault(night, {"feasible": [], "days": defaultdict(list)})
        payload = checks[key]
        if not payload.get("feasible"):
            continue
        bucket["feasible"].append((key[1], key[2]))
        feasible_total += 1
        for day, seconds in payload.get("days", {}).items():
            bucket["days"][day].append((key[1], key[2], seconds))

    days_set = {day for bucket in per_night.values() for day in bucket["days"]}
    days = {}
    for night, bucket in per_night.items():
        if not bucket["feasible"]:
            continue
        feasible_pairs = sorted(bucket["feasible"])
        for day in days_set:
            entries = bucket["days"].get(day, [])
            if not entries:
                # 该夜班当日没有可行组合产生时长，贡献恒为 0，不列入偏移说明。
                continue
            touched = {(item[0], item[1]) for item in entries}
            best = max(entries, key=lambda item: (item[2], -item[0], -item[1]))
            if len(touched) < len(feasible_pairs):
                # 至少一个可行组合当日不产生时长，最低值取 0，并给出该组合。
                untouched = next(pair for pair in feasible_pairs if pair not in touched)
                worst = (untouched[0], untouched[1], 0)
            else:
                worst = min(entries, key=lambda item: (item[2], item[0], item[1]))
            record = days.setdefault(day, {"max": 0, "min": 0, "max_offsets": [], "min_offsets": []})
            record["max"] += best[2]
            record["min"] += worst[2]
            record["max_offsets"].append({"night": night, "hotline": best[0], "inspection": best[1]})
            record["min_offsets"].append({"night": night, "hotline": worst[0], "inspection": worst[1]})

    day_rows = [
        {
            "date": day,
            "max_fixture_minutes": _minutes(record["max"]),
            "max_offsets": record["max_offsets"],
            "max_offset_text": "；".join(_entry_text(item) for item in record["max_offsets"]),
            "min_fixture_minutes": _minutes(record["min"]),
            "min_offsets": record["min_offsets"],
            "min_offset_text": "；".join(_entry_text(item) for item in record["min_offsets"]),
        }
        for day, record in sorted(days.items())
    ]

    night_rows, unsolved = [], []
    for night_plan in night_plans:
        bucket = per_night.get(night_plan["night"], {"feasible": []})
        if night_plan["structural"]:
            status, reason = "无解", night_plan["structural"]
        elif bucket["feasible"]:
            status, reason = "有解", ""
        else:
            status, reason = "无解", "校时范围内没有可行的偏移组合"
        row = {
            "night": night_plan["night"],
            "off_count": night_plan["off_count"],
            "on_count": night_plan["on_count"],
            "combinations": night_plan["combinations"],
            "feasible": len(bucket["feasible"]),
            "status": status,
            "reason": reason,
        }
        night_rows.append(row)
        if status == "无解":
            unsolved.append(row)

    return {
        "complete": True,
        "days": day_rows,
        "nights": night_rows,
        "unsolved_nights": unsolved,
        "combinations": {"total": len(expected), "feasible": feasible_total},
    }


def _batches(items, size=BATCH):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def execute(snapshot, cache, wait=0.0, progress=None):
    """复用已验组合、补验缺失组合；等全部就绪后才汇总出一份时长结果。

    cache 需提供 acquire/release/load/put_many。拿不到校验锁时说明另有进程
    正在验组合，本进程只等待，不产出部分结果；全部组合落库后才汇总。
    """
    night_plans = plan(snapshot)
    lookup = {night_plan["night"]: night_plan for night_plan in night_plans["nights"]}
    expected = [
        (night_plan["night"], hotline, inspection)
        for night_plan in night_plans["nights"] if not night_plan["structural"]
        for hotline, inspection in combinations(snapshot, night_plan)
    ]
    if not expected:
        summary = aggregate(snapshot, night_plans["nights"], {})
        summary["combinations"].update(verified=0, reused=0)
        return summary

    runtime = compile_events(snapshot)
    deadline = time.monotonic() + max(0.0, wait)
    verified = 0
    checks = {}
    while True:
        if cache.acquire():
            try:
                checks = cache.load()
                missing = [key for key in expected if key not in checks]
                if progress:
                    progress(len(expected) - len(missing), len(expected))
                for batch in _batches(missing):
                    payloads = [(key, verify(runtime, lookup[key[0]], key[1], key[2])) for key in batch]
                    cache.put_many(payloads)
                    verified += len(payloads)
                    if progress:
                        progress(len(expected) - len(missing) + verified, len(expected))
                checks = cache.load()
            finally:
                cache.release()
            break
        checks = cache.load()
        if progress:
            progress(sum(1 for key in expected if key in checks), len(expected))
        if all(key in checks for key in expected):
            break
        if time.monotonic() >= deadline:
            pending = sum(1 for key in expected if key not in checks)
            raise Incomplete(f"仍有 {pending} 个偏移组合待校验，等待另一进程完成后汇总")
        time.sleep(0.2)

    summary = aggregate(snapshot, night_plans["nights"], checks)
    if not summary.get("complete"):
        raise Incomplete(f"仍有 {summary.get('pending', 0)} 个偏移组合待校验")
    summary["combinations"].update(verified=verified, reused=len(expected) - verified)
    return summary


def present(summary):
    """把时长汇总转成结果页表格、指标和说明。"""
    tables = [
        {
            "title": "每日停灯盏分钟（按实际日期）",
            "columns": [
                {"key": "date", "label": "日期"},
                {"key": "max_fixture_minutes", "label": "最高停灯盏分钟"},
                {"key": "max_offset_text", "label": "最高值偏移"},
                {"key": "min_fixture_minutes", "label": "最低停灯盏分钟"},
                {"key": "min_offset_text", "label": "最低值偏移"},
            ],
            "rows": summary["days"],
        },
        {
            "title": "夜班校时求解",
            "columns": [
                {"key": "night", "label": "夜班"},
                {"key": "off_count", "label": "熄灯数"},
                {"key": "on_count", "label": "复亮数"},
                {"key": "combinations", "label": "偏移组合"},
                {"key": "feasible", "label": "可行组合"},
                {"key": "status", "label": "结论"},
                {"key": "reason", "label": "说明"},
            ],
            "rows": summary["nights"],
        },
    ]
    if summary["unsolved_nights"]:
        tables.append({
            "title": "无解夜班（单列，不参与每日最高最低值）",
            "columns": [
                {"key": "night", "label": "夜班"},
                {"key": "off_count", "label": "熄灯数"},
                {"key": "on_count", "label": "复亮数"},
                {"key": "combinations", "label": "偏移组合"},
                {"key": "reason", "label": "无解原因"},
            ],
            "rows": summary["unsolved_nights"],
        })
    metrics = [
        {"label": "停灯日期", "value": len(summary["days"]), "detail": "跨午夜的时长按实际日期拆分"},
        {"label": "可行偏移组合", "value": summary["combinations"]["feasible"],
         "detail": f"共 {summary['combinations']['total']} 个组合，复用 {summary['combinations']['reused']} 个已验组合"},
        {"label": "无解夜班", "value": len(summary["unsolved_nights"]),
         "detail": "无解夜班单列，不参与每日最高最低值"},
    ]
    if summary["days"]:
        highest = max(summary["days"], key=lambda row: row["max_fixture_minutes"])
        text = (f"停灯盏分钟最高出现在 {highest['date']}，为 {highest['max_fixture_minutes']}，"
                f"偏移 {highest['max_offset_text']}；最低值见每日明细表。")
    else:
        text = "本期没有可行偏移组合，未生成每日停灯盏分钟。"
    if summary["unsolved_nights"]:
        text += f" 无解夜班 {len(summary['unsolved_nights'])} 个，已在单列表中说明原因。"
    return {"tables": tables, "metrics": metrics, "sections": [{"title": "夜间停灯校时说明", "text": text}]}


def headline(summary):
    """在集中度标题后补一句夜间停灯结论。"""
    solved = sum(1 for row in summary["nights"] if row["status"] == "有解")
    return f" 夜间停灯 {solved}/{len(summary['nights'])} 个夜班有解，覆盖 {len(summary['days'])} 个实际日期。"


def merge(result, summary):
    fragment = present(summary)
    result["metrics"].extend(fragment["metrics"])
    result["tables"].extend(fragment["tables"])
    result["sections"].extend(fragment["sections"])
    result["outage"] = summary
    return result


class MemoryCache:
    """纯计算与测试使用的组合缓存。"""

    def __init__(self, initial=None):
        self._values = dict(initial or {})

    def acquire(self):
        return True

    def release(self):
        pass

    def load(self):
        return dict(self._values)

    def put_many(self, payloads):
        self._values.update(dict(payloads))


def summarize(rows, cache=None, wait=0.0):
    snapshot = build_snapshot(rows)
    return execute(snapshot, cache or MemoryCache(), wait=wait), snapshot
