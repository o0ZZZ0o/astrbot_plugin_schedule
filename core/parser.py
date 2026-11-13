from __future__ import annotations

import asyncio
import json
import re
import ssl
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    DEFAULT_TIME_SLOTS,
    CourseRule,
    ScheduleData,
    TimeSlot,
    get_tz,
    infer_nodes_from_time,
    now_in_tz,
)

COURSE_PALETTE = [
    "#4F46E5",  # Indigo
    "#0D9488",  # Teal
    "#EA580C",  # Orange
    "#2563EB",  # Blue
    "#7C3AED",  # Violet
    "#DB2777",  # Pink
    "#059669",  # Emerald
    "#D97706",  # Amber
    "#DC2626",  # Red
    "#0284C7",  # Sky
]

BYDAY_MAP = {
    "MO": 1,
    "TU": 2,
    "WE": 3,
    "TH": 4,
    "FR": 5,
    "SA": 6,
    "SU": 7,
}


def pick_course_color(course_name: str, index: int = 0) -> str:
    h = sum(ord(ch) for ch in course_name) + index
    return COURSE_PALETTE[h % len(COURSE_PALETTE)]


def extract_wakeup_key(text: str) -> Optional[str]:
    """从用户输入文本中提取 WakeUp 课程表 32 位分享口令"""
    if not text:
        return None
    text = text.strip()
    # 1. 优先匹配「...」中的 32 位口令
    patterns = [
        r"分享口令为\s*[「『\"'【\[]?\s*([0-9a-fA-F]{32})\s*[」』\"'】\]]?",
        r"口令\s*[为：:]?\s*[「『\"'【\[]?\s*([0-9a-fA-F]{32})\s*[」』\"'】\]]?",
        r"[「『]([\da-fA-F]{32})[」』]",
        r"\b([0-9a-fA-F]{32})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).lower()
    return None


async def http_get_text(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 12
) -> str:
    """通用异步 HTTP GET 文本获取（优先使用 aiohttp，回退 urllib）"""
    req_headers = {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        req_headers.update(headers)

    try:
        import aiohttp  # type: ignore

        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers=req_headers, ssl=False) as resp:
                resp.raise_for_status()
                return await resp.text(encoding="utf-8", errors="replace")
    except ImportError:
        pass

    def _sync_fetch() -> str:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
            raw = response.read()
            return raw.decode("utf-8", errors="replace")

    return await asyncio.to_thread(_sync_fetch)


async def fetch_and_parse_wakeup(
    share_input: str,
    api_url: str = "https://i.wakeup.fun/share_schedule/get",
    tz_name: str = "Asia/Shanghai",
) -> ScheduleData:
    """通过 WakeUp 分享口令或原始数据解析课表"""
    stripped = share_input.strip()
    # 如果直接传入了 5 行 wakeup_schedule 内容或 JSON
    if stripped.startswith("{") and ("\n[" in stripped or "courseList" in stripped):
        return parse_wakeup_raw_data(stripped, tz_name=tz_name)

    key = extract_wakeup_key(stripped)
    if not key:
        raise ValueError("未能从输入中识别出有效的 32 位 WakeUp 课程表分享口令。")

    endpoints = [
        api_url.rstrip("/"),
        "https://i.wakeup.fun/share_schedule/get",
        "https://api.wakeup.fun/share_schedule/get",
    ]
    # 去重保序
    unique_endpoints: List[str] = []
    for ep in endpoints:
        if ep and ep not in unique_endpoints:
            unique_endpoints.append(ep)

    last_err: Optional[Exception] = None
    for ep in unique_endpoints:
        sep = "&" if "?" in ep else "?"
        url = f"{ep}{sep}key={urllib.parse.quote(key)}"
        try:
            resp_text = await http_get_text(
                url,
                headers={
                    "User-Agent": "okhttp/4.12.0",
                    "version": "240",
                    "Accept": "application/json",
                },
                timeout=12,
            )
            payload = json.loads(resp_text)
            status = payload.get("status")
            code = payload.get("code")
            data = payload.get("data")
            if (status in (1, 200) or code == 200 or payload.get("success")) and data:
                if isinstance(data, str):
                    return parse_wakeup_raw_data(data, tz_name=tz_name)
                elif isinstance(data, dict):
                    return parse_wakeup_dict_data(data, tz_name=tz_name)
            msg = payload.get("message") or payload.get("msg") or "口令可能已过期（有效期30分钟）"
            last_err = ValueError(f"WakeUp 接口返回失败: {msg}")
        except Exception as e:
            last_err = e

    raise ValueError(f"无法获取 WakeUp 课表数据：{last_err}")


def parse_wakeup_raw_data(raw_data: str, tz_name: str = "Asia/Shanghai") -> ScheduleData:
    """解析 WakeUp 的 5 行 JSON 格式 (.wakeup_schedule) 或单 JSON 格式"""
    lines = [line.strip() for line in raw_data.strip().splitlines() if line.strip()]
    if len(lines) == 1:
        obj = json.loads(lines[0])
        if isinstance(obj, dict) and "data" in obj and isinstance(obj["data"], str):
            return parse_wakeup_raw_data(obj["data"], tz_name=tz_name)
        if isinstance(obj, dict):
            return parse_wakeup_dict_data(obj, tz_name=tz_name)

    if len(lines) < 5:
        raise ValueError("WakeUp 课表数据格式不完整（少于 5 个数据段）。")

    # Line 0: base config
    # Line 1: time slots array
    # Line 2: table config (startDate, maxWeek, nodes, tableName, sundayFirst)
    # Line 3: course meta array (id, courseName, color)
    # Line 4: course details array (id, day, startNode, step, startWeek, endWeek, type, room, teacher)
    time_list_raw = json.loads(lines[1])
    table_cfg = json.loads(lines[2])
    course_meta_list = json.loads(lines[3])
    course_detail_list = json.loads(lines[4])

    time_slots: List[TimeSlot] = []
    time_map: Dict[int, TimeSlot] = {}
    for item in time_list_raw:
        node = int(item.get("node", 0))
        st = str(item.get("startTime", "08:00")).strip()
        et = str(item.get("endTime", "08:45")).strip()
        if node >= 1 and st and et and st != "00:00":
            slot = TimeSlot(node=node, start_time=st, end_time=et)
            if node not in time_map:
                time_map[node] = slot
                time_slots.append(slot)

    if not time_slots:
        time_slots = [TimeSlot.from_dict(x) for x in DEFAULT_TIME_SLOTS]
        time_map = {s.node: s for s in time_slots}
    else:
        time_slots.sort(key=lambda x: x.node)

    raw_start_date = str(table_cfg.get("startDate", "2026-09-07")).strip()
    start_date_str = _normalize_date_str(raw_start_date)
    max_week = max(1, int(table_cfg.get("maxWeek", 20)))
    max_nodes = max(1, int(table_cfg.get("nodes", 12)))
    table_name = str(table_cfg.get("tableName") or "WakeUp课表").strip()

    meta_by_id: Dict[int, Dict[str, Any]] = {}
    for m in course_meta_list:
        cid = int(m.get("id", -1))
        meta_by_id[cid] = m

    courses: List[CourseRule] = []
    for idx, d in enumerate(course_detail_list):
        cid = int(d.get("id", -1))
        meta = meta_by_id.get(cid, {})
        course_name = str(
            d.get("courseName") or meta.get("courseName") or f"课程{cid}"
        ).strip()
        color = str(meta.get("color") or "").strip()
        if not color or not color.startswith("#") or len(color) > 7:
            color = pick_course_color(course_name, idx)

        day = int(d.get("day", 1))
        start_node = max(1, int(d.get("startNode", 1)))
        step = max(1, int(d.get("step", 1)))
        end_node = start_node + step - 1

        own_time = bool(d.get("ownTime", False))
        if own_time and d.get("startTime") and d.get("endTime"):
            start_time = str(d["startTime"]).strip()
            end_time = str(d["endTime"]).strip()
        else:
            st_slot = time_map.get(start_node)
            et_slot = time_map.get(end_node) or time_map.get(start_node)
            start_time = st_slot.start_time if st_slot else "08:00"
            if et_slot and end_node in time_map:
                end_time = et_slot.end_time
            else:
                # 根据每节 45 分钟 + 课间 5 分钟推算
                t_parts = [int(p) for p in start_time.split(":")]
                mins = t_parts[0] * 60 + t_parts[1] + step * 45 + max(0, step - 1) * 5
                end_time = f"{(mins // 60) % 24:02d}:{mins % 60:02d}"

        start_week = max(1, int(d.get("startWeek", 1)))
        end_week = max(start_week, int(d.get("endWeek", max_week)))
        week_type = int(d.get("type", 0))  # 0=全周, 1=单周, 2=双周
        room = str(d.get("room") or "").strip()
        teacher = str(d.get("teacher") or "").strip()

        if end_node > max_nodes:
            max_nodes = end_node

        courses.append(
            CourseRule(
                course_name=course_name,
                teacher=teacher,
                room=room,
                weekday=day,
                start_node=start_node,
                step=step,
                start_time=start_time,
                end_time=end_time,
                start_week=start_week,
                end_week=end_week,
                week_type=week_type,
                color=color,
            )
        )

    return ScheduleData(
        source_type="wakeup",
        table_name=table_name,
        start_date=start_date_str,
        max_week=max_week,
        max_nodes=max_nodes,
        time_slots=time_slots,
        courses=courses,
        updated_at=now_in_tz(tz_name).strftime("%Y-%m-%d %H:%M:%S"),
    )


def parse_wakeup_dict_data(data: Dict[str, Any], tz_name: str = "Asia/Shanghai") -> ScheduleData:
    """解析单字典结构的 WakeUp 课表 JSON"""
    start_date_str = _normalize_date_str(str(data.get("startDate", "2026-09-07")))
    table_name = str(data.get("tableName") or "WakeUp课表")
    max_week = int(data.get("maxWeek", 20))
    max_nodes = int(data.get("nodes", 12))

    raw_times = data.get("timeList") or DEFAULT_TIME_SLOTS
    time_slots = [TimeSlot.from_dict(t) for t in raw_times]
    time_map = {s.node: s for s in time_slots}

    courses: List[CourseRule] = []
    for idx, item in enumerate(data.get("courseList", [])):
        course_name = str(item.get("courseName", f"课程{idx + 1}")).strip()
        day = int(item.get("day", 1))
        start_node = int(item.get("startNode", 1))
        step = max(1, int(item.get("step", 2)))
        end_node = start_node + step - 1
        st_slot = time_map.get(start_node)
        et_slot = time_map.get(end_node)
        start_time = str(item.get("startTime") or (st_slot.start_time if st_slot else "08:00"))
        end_time = str(item.get("endTime") or (et_slot.end_time if et_slot else "09:35"))
        start_week = int(item.get("startWeek", 1))
        end_week = int(item.get("endWeek", max_week))
        week_type = int(item.get("type", 0))

        # 兼容 stepWeekList
        step_weeks = item.get("stepWeekList")
        if isinstance(step_weeks, list) and step_weeks:
            start_week = min(int(w) for w in step_weeks)
            end_week = max(int(w) for w in step_weeks)

        courses.append(
            CourseRule(
                course_name=course_name,
                teacher=str(item.get("teacher", "")).strip(),
                room=str(item.get("room", "")).strip(),
                weekday=day,
                start_node=start_node,
                step=step,
                start_time=start_time,
                end_time=end_time,
                start_week=start_week,
                end_week=end_week,
                week_type=week_type,
                color=pick_course_color(course_name, idx),
            )
        )

    return ScheduleData(
        source_type="wakeup",
        table_name=table_name,
        start_date=start_date_str,
        max_week=max_week,
        max_nodes=max_nodes,
        time_slots=time_slots,
        courses=courses,
        updated_at=now_in_tz(tz_name).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _normalize_date_str(raw: str) -> str:
    """将 2026-9-7 或 2026/09/07 规范化为 YYYY-MM-DD"""
    raw = raw.strip().replace("/", "-").split(" ")[0]
    parts = [int(p) for p in raw.split("-") if p.isdigit()]
    if len(parts) >= 3:
        return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    return date.today().isoformat()


def _unfold_ics_lines(ics_text: str) -> List[str]:
    """按照 RFC 5545 展开折行（以空格或 Tab 开头的续行）"""
    raw_lines = ics_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded: List[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _unescape_ics_text(val: str) -> str:
    return (
        val.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _parse_ics_datetime(
    val: str, params: Dict[str, str], target_tz_name: str = "Asia/Shanghai"
) -> datetime:
    """解析 ICS 中的 DTSTART / DTEND / UNTIL 时间并统一转换至目标时区"""
    val = val.strip()
    target_tz = get_tz(target_tz_name)
    tzid = params.get("TZID", "")
    source_tz = get_tz(tzid) if tzid else target_tz

    is_utc = val.endswith("Z")
    if is_utc:
        val = val[:-1]

    if "T" in val:
        dt_part = val.replace("-", "").replace(":", "")
        fmt = "%Y%m%dT%H%M%S" if len(dt_part) >= 15 else "%Y%m%dT%H%M"
        naive_dt = datetime.strptime(dt_part[:15], fmt)
        if is_utc:
            aware_dt = naive_dt.replace(tzinfo=timezone.utc).astimezone(target_tz)
        else:
            aware_dt = naive_dt.replace(tzinfo=source_tz).astimezone(target_tz)
        return aware_dt
    else:
        d_part = val.replace("-", "")[:8]
        naive_d = datetime.strptime(d_part, "%Y%m%d")
        return naive_d.replace(hour=8, minute=0, tzinfo=target_tz)


def _parse_ics_duration(dur_str: str) -> timedelta:
    """解析 RFC 5545 DURATION 如 PT1H40M 或 PT90M"""
    dur_str = dur_str.strip().upper()
    days = hours = minutes = seconds = 0
    m_d = re.search(r"(\d+)D", dur_str)
    m_h = re.search(r"(\d+)H", dur_str)
    m_m = re.search(r"(\d+)M", dur_str)
    m_s = re.search(r"(\d+)S", dur_str)
    if m_d:
        days = int(m_d.group(1))
    if m_h:
        hours = int(m_h.group(1))
    if m_m:
        minutes = int(m_m.group(1))
    if m_s:
        seconds = int(m_s.group(1))
    td = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    return td if td.total_seconds() > 0 else timedelta(minutes=90)


def _extract_meta_from_ics_fields(
    summary: str, location: str, description: str
) -> Tuple[str, str, str, Optional[int], Optional[int]]:
    """从 SUMMARY / LOCATION / DESCRIPTION 中智能提取 (课程名, 教室, 教师, 起始节, 节数)"""
    course_name = summary.strip()
    room = location.strip()
    teacher = ""
    start_node: Optional[int] = None
    step: Optional[int] = None

    # 如果 SUMMARY 包含 @教室，如 "高等数学@教三-201"
    if "@" in course_name and not room:
        parts = course_name.split("@", 1)
        course_name = parts[0].strip()
        room = parts[1].strip()

    if description:
        desc_lines = [ln.strip() for ln in description.splitlines() if ln.strip()]
        for ln in desc_lines:
            # 匹配节次：如 "第1 - 2节" 或 "第3-4节" 或 "1-2节"
            m_node = re.search(r"第?\s*(\d+)\s*[-~至到]\s*(\d+)\s*节", ln)
            if m_node:
                n1, n2 = int(m_node.group(1)), int(m_node.group(2))
                if 1 <= n1 <= n2 <= 20:
                    start_node = n1
                    step = n2 - n1 + 1
                continue
            m_single = re.search(r"第\s*(\d+)\s*节", ln)
            if m_single and start_node is None:
                start_node = int(m_single.group(1))
                step = 1
                continue

            # 匹配教师/教室键值对
            m_teacher = re.match(r"^(?:教师|老师|任课教师|授课教师|Teacher)[：:\s]+(.+)$", ln, re.I)
            if m_teacher:
                teacher = m_teacher.group(1).strip()
                continue
            m_room = re.match(r"^(?:教室|地点|上课地点|位置|Location|Room)[：:\s]+(.+)$", ln, re.I)
            if m_room and not room:
                room = m_room.group(1).strip()
                continue

        # WakeUp 导出的 .ics DESCRIPTION 常见三行格式：
        # 第1 - 2节 \n 教三201 \n 张老师
        if not teacher and len(desc_lines) >= 2:
            filtered = [
                ln
                for ln in desc_lines
                if not re.search(r"第?\s*\d+.*节|周", ln) and ln != room
            ]
            if filtered:
                candidate = filtered[-1]
                if len(candidate) <= 20:
                    teacher = candidate

    return course_name, room, teacher, start_node, step


def _expand_rrule_dates(
    start_dt: datetime,
    rrule_str: str,
    exdates: List[str],
    target_tz_name: str = "Asia/Shanghai",
) -> List[date]:
    """展开 ICS 的 RRULE 重复规则为具体日期列表"""
    parts: Dict[str, str] = {}
    for seg in rrule_str.split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip().upper()] = v.strip()

    freq = parts.get("FREQ", "WEEKLY").upper()
    interval = max(1, int(parts.get("INTERVAL", "1")))
    count = int(parts["COUNT"]) if "COUNT" in parts and parts["COUNT"].isdigit() else None
    until_dt: Optional[datetime] = None
    if "UNTIL" in parts:
        try:
            until_dt = _parse_ics_datetime(parts["UNTIL"], {}, target_tz_name)
        except Exception:
            until_dt = None

    byday_raw = parts.get("BYDAY", "")
    bydays: List[int] = []
    if byday_raw:
        for token in byday_raw.split(","):
            code = token.strip()[-2:].upper()
            if code in BYDAY_MAP:
                bydays.append(BYDAY_MAP[code])

    ex_set = set(exdates)
    dates: List[date] = []
    max_occurrences = count if count is not None else 30

    if freq == "WEEKLY":
        base_date = start_dt.date()
        base_monday = base_date - timedelta(days=base_date.isoweekday() - 1)
        target_weekdays = sorted(set(bydays)) if bydays else [base_date.isoweekday()]
        week_idx = 0
        while len(dates) < max_occurrences and week_idx < 40:
            week_monday = base_monday + timedelta(weeks=week_idx * interval)
            for wd in target_weekdays:
                d = week_monday + timedelta(days=wd - 1)
                if d < base_date:
                    continue
                if until_dt and d > until_dt.date():
                    return dates
                d_str = d.isoformat()
                if d_str not in ex_set:
                    dates.append(d)
                    if len(dates) >= max_occurrences:
                        return dates
            week_idx += 1
    elif freq == "DAILY":
        cur_d = start_dt.date()
        step_idx = 0
        while len(dates) < max_occurrences and step_idx < 200:
            d = cur_d + timedelta(days=step_idx * interval)
            if until_dt and d > until_dt.date():
                break
            if d.isoformat() not in ex_set:
                dates.append(d)
            step_idx += 1
    else:
        if start_dt.date().isoformat() not in ex_set:
            dates.append(start_dt.date())

    return dates


def parse_ics_content(
    ics_text: str,
    tz_name: str = "Asia/Shanghai",
    table_name: str = "ICS日历课表",
) -> ScheduleData:
    """解析 .ics 日历文本为统一的 ScheduleData（内置完整 RFC 5545 解析并自动转换时区）"""
    if "BEGIN:VCALENDAR" not in ics_text.upper():
        raise ValueError("无效的 .ics 文件内容（缺少 BEGIN:VCALENDAR）。")

    lines = _unfold_ics_lines(ics_text)
    in_event = False
    current_props: List[Tuple[str, Dict[str, str], str]] = []
    raw_events: List[List[Tuple[str, Dict[str, str], str]]] = []
    cal_name = table_name

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        upper = line_stripped.upper()
        if upper == "BEGIN:VEVENT":
            in_event = True
            current_props = []
            continue
        elif upper == "END:VEVENT":
            if in_event and current_props:
                raw_events.append(current_props)
            in_event = False
            current_props = []
            continue

        if ":" not in line_stripped:
            continue
        key_part, val_part = line_stripped.split(":", 1)
        segments = key_part.split(";")
        prop_name = segments[0].strip().upper()
        params: Dict[str, str] = {}
        for seg in segments[1:]:
            if "=" in seg:
                pk, pv = seg.split("=", 1)
                params[pk.strip().upper()] = pv.strip().strip('"')

        if not in_event:
            if prop_name in ("X-WR-CALNAME", "PRODID") and prop_name == "X-WR-CALNAME":
                if val_part.strip():
                    cal_name = _unescape_ics_text(val_part)
        else:
            current_props.append((prop_name, params, val_part))

    if not raw_events:
        raise ValueError(".ics 文件中未找到任何课程事件 (VEVENT)。")

    # 聚合相同 (课程名, 星期, start_time, end_time, 教室, 教师) 的日期
    grouped_rules: Dict[Tuple[str, int, str, str, str, str], Dict[str, Any]] = {}
    all_dates: List[date] = []
    max_nodes = 12

    for props in raw_events:
        summary = ""
        location = ""
        description = ""
        dtstart: Optional[datetime] = None
        dtend: Optional[datetime] = None
        duration_td: Optional[timedelta] = None
        rrule_str = ""
        exdates: List[str] = []

        for pname, params, pval in props:
            if pname == "SUMMARY":
                summary = _unescape_ics_text(pval)
            elif pname == "LOCATION":
                location = _unescape_ics_text(pval)
            elif pname == "DESCRIPTION":
                description = _unescape_ics_text(pval)
            elif pname == "DTSTART":
                dtstart = _parse_ics_datetime(pval, params, tz_name)
            elif pname == "DTEND":
                dtend = _parse_ics_datetime(pval, params, tz_name)
            elif pname == "DURATION":
                duration_td = _parse_ics_duration(pval)
            elif pname == "RRULE":
                rrule_str = pval.strip()
            elif pname == "EXDATE":
                for ex_item in pval.split(","):
                    ex_item = ex_item.strip()
                    if ex_item:
                        try:
                            ex_dt = _parse_ics_datetime(ex_item, params, tz_name)
                            exdates.append(ex_dt.date().isoformat())
                        except Exception:
                            pass

        if not dtstart or not summary:
            continue
        if not dtend:
            dtend = dtstart + (duration_td or timedelta(minutes=90))
        if dtend <= dtstart:
            dtend = dtstart + timedelta(minutes=90)

        course_name, room, teacher, start_node, step = _extract_meta_from_ics_fields(
            summary, location, description
        )
        start_hhmm = dtstart.strftime("%H:%M")
        end_hhmm = dtend.strftime("%H:%M")

        if start_node is None or step is None:
            inferred_start, inferred_step = infer_nodes_from_time(start_hhmm, end_hhmm)
            start_node = start_node or inferred_start
            step = step or inferred_step

        if start_node + step - 1 > max_nodes:
            max_nodes = start_node + step - 1

        if rrule_str:
            occ_dates = _expand_rrule_dates(dtstart, rrule_str, exdates, tz_name)
        else:
            d_str = dtstart.date().isoformat()
            occ_dates = [] if d_str in exdates else [dtstart.date()]

        for occ_d in occ_dates:
            all_dates.append(occ_d)
            wd = occ_d.isoweekday()
            gkey = (course_name, wd, start_hhmm, end_hhmm, room, teacher)
            if gkey not in grouped_rules:
                grouped_rules[gkey] = {
                    "course_name": course_name,
                    "teacher": teacher,
                    "room": room,
                    "weekday": wd,
                    "start_node": start_node,
                    "step": step,
                    "start_time": start_hhmm,
                    "end_time": end_hhmm,
                    "dates": set(),
                }
            grouped_rules[gkey]["dates"].add(occ_d.isoformat())

    if not grouped_rules:
        raise ValueError("未能从 .ics 文件中解析出有效课程。")

    earliest_date = min(all_dates) if all_dates else date.today()
    semester_monday = earliest_date - timedelta(days=earliest_date.isoweekday() - 1)
    latest_date = max(all_dates) if all_dates else earliest_date
    total_weeks = max(20, ((latest_date - semester_monday).days // 7) + 1)

    courses: List[CourseRule] = []
    for idx, (_, info) in enumerate(grouped_rules.items()):
        sorted_dates = sorted(info["dates"])
        week_nums = [
            ((date.fromisoformat(ds) - semester_monday).days // 7) + 1
            for ds in sorted_dates
        ]
        min_w = max(1, min(week_nums)) if week_nums else 1
        max_w = max(min_w, max(week_nums)) if week_nums else 20

        courses.append(
            CourseRule(
                course_name=info["course_name"],
                teacher=info["teacher"],
                room=info["room"],
                weekday=info["weekday"],
                start_node=info["start_node"],
                step=info["step"],
                start_time=info["start_time"],
                end_time=info["end_time"],
                start_week=min_w,
                end_week=max_w,
                week_type=0,
                specific_dates=sorted_dates,
                color=pick_course_color(info["course_name"], idx),
            )
        )

    return ScheduleData(
        source_type="ics",
        table_name=cal_name,
        start_date=semester_monday.isoformat(),
        max_week=total_weeks,
        max_nodes=max_nodes,
        time_slots=[TimeSlot.from_dict(x) for x in DEFAULT_TIME_SLOTS],
        courses=courses,
        updated_at=now_in_tz(tz_name).strftime("%Y-%m-%d %H:%M:%S"),
    )
