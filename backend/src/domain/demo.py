"""临汀市虚拟路灯台账、已归并的暗区事件与夜间停灯记录。"""


_SITES = (
    ("滨河片区", "柳汀大道北段", 32, (14, 14, 15, 16, 17, 18, 19, 20)),
    ("老城片区", "东门巷", 5, (14, 16, 17, 19, 20)),
    ("滨河片区", "汀桥路公交站段", 24, (15, 17, 18, 20)),
    ("滨河片区", "长堤辅道", 3, (16, 20)),
    ("老城片区", "西仓里路口", 2, (19,)),
    ("滨河片区", "渡口支路", 9, ()),
    ("滨河片区", "桐浦堤下通道", 6, ()),
    ("老城片区", "埠头街南段", 14, ()),
    ("老城片区", "石平码头广场路", 11, ()),
    ("老城片区", "钟楼西街", 17, ()),
)

# 记录方本机时间比实际时间：热线快 3 分钟，巡检慢 2 分钟。校时范围按整分钟枚举。
_RANGES = (
    ("2026-11-18", "热线", 2, 4), ("2026-11-18", "巡检", -3, -1),
    ("2026-11-19", "热线", 2, 4), ("2026-11-19", "巡检", -3, -1),
    ("2026-11-20", "热线", 2, 4), ("2026-11-20", "巡检", -3, -1),
    ("2026-11-16", "热线", 0, 0), ("2026-11-16", "巡检", 0, 0),
    ("2026-11-17", "热线", 2, 4), ("2026-11-17", "巡检", -3, -1),
)

_EVENTS = (
    # 夜班, 灯组, 熄灯记录时间, 复亮记录时间（均为记录方本机时间）
    ("2026-11-18", "LH-01", "2026-11-18T22:27", "2026-11-18T23:47"),
    ("2026-11-18", "LH-04", "2026-11-18T23:47", "2026-11-19T00:27"),
    ("2026-11-19", "LH-01", "2026-11-19T20:57", "2026-11-19T22:02"),
    ("2026-11-19", "LH-02", "2026-11-19T23:17", "2026-11-20T00:12"),
    ("2026-11-20", "LH-06", "2026-11-20T21:57", "2026-11-20T23:02"),
    # 11-16：范围固定为 0，复亮记录早于熄灯，范围内没有可行组合。
    ("2026-11-16", "LH-05", "2026-11-16T23:00", "2026-11-16T22:50"),
    # 11-17：LH-01 有两次熄灯、只有一次复亮，同组记录数不一致。
    ("2026-11-17", "LH-01", "2026-11-17T22:10", "2026-11-17T23:20"),
)

_EXTRA_OFF = (
    ("2026-11-17", "LH-01", "2026-11-17T23:40"),
)


def _blank(night):
    return {
        "night": night, "lane": None, "offset_min": None, "offset_max": None,
        "recorded_at": None, "district": None, "site_name": None, "fixture_count": None,
        "incident_id": None, "report_date": None,
    }


def demo_rows() -> list[dict]:
    rows = []
    for number, (district, site, fixtures, days) in enumerate(_SITES, start=1):
        site_id = f"LH-{number:02d}"
        rows.append({
            "record_type": "灯组台账", "site_id": site_id, "district": district,
            "site_name": site, "fixture_count": fixtures, "incident_id": None, "report_date": None,
            "night": None, "lane": None, "offset_min": None, "offset_max": None, "recorded_at": None,
        })
        for sequence, day in enumerate(days, start=1):
            rows.append({
                "record_type": "暗区工单", "site_id": site_id, "district": None,
                "site_name": None, "fixture_count": None,
                "incident_id": f"LT-2611{day}-{number:02d}-{sequence:02d}",
                "report_date": f"2026-11-{day}",
                "night": None, "lane": None, "offset_min": None, "offset_max": None, "recorded_at": None,
            })
    for night, lane, low, high in _RANGES:
        rows.append({**_blank(night), "record_type": "校时范围", "lane": lane,
                     "offset_min": low, "offset_max": high})
    for night, site, off, on in _EVENTS:
        rows.append({**_blank(night), "record_type": "熄灯记录", "site_id": site, "recorded_at": off})
        rows.append({**_blank(night), "record_type": "复亮记录", "site_id": site, "recorded_at": on})
    for night, site, off in _EXTRA_OFF:
        rows.append({**_blank(night), "record_type": "熄灯记录", "site_id": site, "recorded_at": off})
    return rows
