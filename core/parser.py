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
    build_dynamic_time_slots_from_courses,
    build_slots_for_unnumbered_events,
    get_tz,
    hhmm_to_minutes,
    infer_nodes_from_time,
    infer_unit_and_break_minutes,
    minutes_to_hhmm,
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


def parse_week_spec_text(text: str) -> Tuple[Optional[int], Optional[int], int, List[int]]:
    """
    从描述文本中解析周数范围与单双周信息，例如：
    - "第1-16周" -> (1, 16, 0, [1..16])
    - "2-16周(双)" / "第2-16双周" -> (2, 16, 2, [2,4..16])
    - "1-8,10-16周" -> (1, 16, 0, [1..8, 10..16])
    - "第5周" -> (5, 5, 0, [5])
    """
    if not text:
        return None, None, 0, []

    # 匹配形如 "第1-8,10-16周(单)" 或 "周次: 1-18周" 或 "1-16周"
    m_week_expr = re.search(
        r"(?:第\s*)?(\d+(?:\s*[-~至到,，、]\s*\d+)*)\s*(?:([单双])\s*)?周(?:\s*[\(（]?\s*([单双])周?\s*[\)）]?)?",
        text,
    )
    if not m_week_expr:
        return None, None, 0, []

    raw_body = m_week_expr.group(1)
    odd_even_tag = m_week_expr.group(2) or m_week_expr.group(3) or ""
    if not odd_even_tag:
        if "单周" in text:
            odd_even_tag = "单"
        elif "双周" in text:
            odd_even_tag = "双"

    week_type = 1 if odd_even_tag == "单" else (2 if odd_even_tag == "双" else 0)
    active_weeks: List[int] = []

    for seg in re.split(r"[,，、]+", raw_body):
        seg = seg.strip()
        if not seg:
            continue
        m_rng = re.match(r"^(\d+)\s*[-~至到]\s*(\d+)$", seg)
        if m_rng:
            w1, w2 = int(m_rng.group(1)), int(m_rng.group(2))
            if 1 <= w1 <= w2 <= 40:
                for w in range(w1, w2 + 1):
                    if week_type == 1 and w % 2 == 0:
                        continue
                    if week_type == 2 and w % 2 == 1:
                        continue
                    active_weeks.append(w)
        elif seg.isdigit():
            w = int(seg)
            if 1 <= w <= 40:
                active_weeks.append(w)

    active_weeks = sorted(set(active_weeks))
    if not active_weeks:
        return None, None, 0, []
    return min(active_weeks), max(active_weeks), week_type, active_weeks


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

    time_list_raw = json.loads(lines[1])
    table_cfg = json.loads(lines[2])
    course_meta_list = json.loads(lines[3])
    course_detail_list = json.loads(lines[4])

    raw_start_date = str(table_cfg.get("startDate", "2026-09-07")).strip()
    start_date_str = _normalize_date_str(raw_start_date)
    cfg_max_week = max(1, int(table_cfg.get("maxWeek", 20)))
    cfg_max_nodes = max(1, int(table_cfg.get("nodes", 12)))
    table_name = str(table_cfg.get("tableName") or "WakeUp课表").strip()

    # 计算实际课程涉及的最大节次
    max_course_node = 0
    for d in course_detail_list:
        sn = max(1, int(d.get("startNode", 1)))
        st = max(1, int(d.get("step", 1)))
        if sn + st - 1 > max_course_node:
            max_course_node = sn + st - 1
    effective_max_nodes = max(cfg_max_nodes, max_course_node)

    # 过滤 WakeUp 中未配置的占位节次（例如超过 nodes 后重置为 08:00-08:45 或 00:00-00:00 的无效项）
    time_slots: List[TimeSlot] = []
    time_map: Dict[int, TimeSlot] = {}
    last_end_min = -1
    for item in sorted(time_list_raw, key=lambda x: int(x.get("node", 0))):
        node = int(item.get("node", 0))
        st = str(item.get("startTime", "")).strip()
        et = str(item.get("endTime", "")).strip()
        if node < 1 or node > effective_max_nodes or not st or not et or st == "00:00":
            continue
        st_m = hhmm_to_minutes(st)
        et_m = hhmm_to_minutes(et)
        if et_m <= st_m:
            continue
        if last_end_min >= 0 and st_m < last_end_min:
            continue
        slot = TimeSlot(node=node, start_time=st, end_time=et)
        if node not in time_map:
            time_map[node] = slot
            time_slots.append(slot)
            last_end_min = et_m

    if not time_slots:
        time_slots = [
            TimeSlot.from_dict(x)
            for x in DEFAULT_TIME_SLOTS[: min(effective_max_nodes, len(DEFAULT_TIME_SLOTS))]
        ]
        time_map = {s.node: s for s in time_slots}

    unit_mins, break_mins = infer_unit_and_break_minutes([], time_slots)

    meta_by_id: Dict[int, Dict[str, Any]] = {}
    for m in course_meta_list:
        cid = int(m.get("id", -1))
        meta_by_id[cid] = m

    courses: List[CourseRule] = []
    max_observed_week = cfg_max_week
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
        if day == 0:
            day = 7
        start_node = max(1, int(d.get("startNode", 1)))
        step = max(1, int(d.get("step", 1)))
        end_node = start_node + step - 1

        own_time = bool(d.get("ownTime", False))
        if own_time and d.get("startTime") and d.get("endTime"):
            start_time = str(d["startTime"]).strip()
            end_time = str(d["endTime"]).strip()
        else:
            st_slot = time_map.get(start_node)
            et_slot = time_map.get(end_node)
            if st_slot:
                start_time = st_slot.start_time
            elif time_slots:
                first_s = time_slots[0]
                st_m = hhmm_to_minutes(first_s.start_time) + (start_node - first_s.node) * (
                    unit_mins + break_mins
                )
                start_time = minutes_to_hhmm(st_m)
            else:
                start_time = "08:00"

            if et_slot:
                end_time = et_slot.end_time
            else:
                st_m = hhmm_to_minutes(start_time)
                et_m = st_m + step * unit_mins + max(0, step - 1) * break_mins
                end_time = minutes_to_hhmm(et_m)

        start_week = max(1, int(d.get("startWeek", 1)))
        end_week = max(start_week, int(d.get("endWeek", cfg_max_week)))
        week_type = int(d.get("type", 0))  # 0=全周, 1=单周, 2=双周
        if end_week > max_observed_week:
            max_observed_week = end_week

        room = str(d.get("room") or "").strip()
        teacher = str(d.get("teacher") or "").strip()

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
        max_week=max_observed_week,
        max_nodes=effective_max_nodes,
        time_slots=time_slots,
        courses=courses,
        updated_at=now_in_tz(tz_name).strftime("%Y-%m-%d %H:%M:%S"),
    )


def parse_wakeup_dict_data(data: Dict[str, Any], tz_name: str = "Asia/Shanghai") -> ScheduleData:
    """解析单字典结构的 WakeUp 课表 JSON"""
    start_date_str = _normalize_date_str(str(data.get("startDate", "2026-09-07")))
    table_name = str(data.get("tableName") or "WakeUp课表")
    cfg_max_week = max(1, int(data.get("maxWeek", 20)))
    cfg_max_nodes = max(1, int(data.get("nodes", 12)))

    raw_times = data.get("timeList") or DEFAULT_TIME_SLOTS
    time_slots = [TimeSlot.from_dict(t) for t in raw_times]
    time_map = {s.node: s for s in time_slots}
    unit_mins, break_mins = infer_unit_and_break_minutes([], time_slots)

    courses: List[CourseRule] = []
    max_observed_week = cfg_max_week
    max_observed_node = cfg_max_nodes

    for idx, item in enumerate(data.get("courseList", [])):
        course_name = str(item.get("courseName", f"课程{idx + 1}")).strip()
        day = int(item.get("day", 1))
        if day == 0:
            day = 7
        start_node = max(1, int(item.get("startNode", 1)))
        step = max(1, int(item.get("step", 2)))
        end_node = start_node + step - 1
        if end_node > max_observed_node:
            max_observed_node = end_node

        st_slot = time_map.get(start_node)
        et_slot = time_map.get(end_node)
        start_time = str(item.get("startTime") or (st_slot.start_time if st_slot else "08:00"))
        if item.get("endTime"):
            end_time = str(item["endTime"])
        elif et_slot:
            end_time = et_slot.end_time
        else:
            st_m = hhmm_to_minutes(start_time)
            end_time = minutes_to_hhmm(st_m + step * unit_mins + max(0, step - 1) * break_mins)

        start_week = max(1, int(item.get("startWeek", 1)))
        end_week = max(start_week, int(item.get("endWeek", cfg_max_week)))
        week_type = int(item.get("type", 0))
        active_weeks: List[int] = []

        step_weeks = item.get("stepWeekList")
        if isinstance(step_weeks, list) and step_weeks:
            active_weeks = sorted({int(w) for w in step_weeks if int(w) >= 1})
            if active_weeks:
                start_week = min(active_weeks)
                end_week = max(active_weeks)

        if end_week > max_observed_week:
            max_observed_week = end_week

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
                active_weeks=active_weeks,
                color=pick_course_color(course_name, idx),
            )
        )

    return ScheduleData(
        source_type="wakeup",
        table_name=table_name,
        start_date=start_date_str,
        max_week=max_observed_week,
        max_nodes=max_observed_node,
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
) -> Tuple[str, str, str, Optional[int], Optional[int], Optional[int], Optional[int], int, List[int]]:
    """
    从 SUMMARY / LOCATION / DESCRIPTION 中智能提取：
    (课程名, 教室, 教师, 起始节, 节数, 起始周, 结束周, 单双周类型, 活跃周列表)
    """
    course_name = summary.strip()
    room = location.strip()
    teacher = ""
    start_node: Optional[int] = None
    step: Optional[int] = None
    start_week: Optional[int] = None
    end_week: Optional[int] = None
    week_type: int = 0
    active_weeks: List[int] = []

    if "@" in course_name and not room:
        parts = course_name.split("@", 1)
        course_name = parts[0].strip()
        room = parts[1].strip()

    combined_text = f"{summary}\n{description}"
    w_min, w_max, w_type, w_list = parse_week_spec_text(combined_text)
    if w_min is not None and w_max is not None:
        start_week = w_min
        end_week = w_max
        week_type = w_type
        active_weeks = w_list

    if description:
        desc_lines = [ln.strip() for ln in description.splitlines() if ln.strip()]
        for ln in desc_lines:
            # 匹配节次范围：如 "第1 - 2节"、"第3-5节"、"1-2节"、"节次: 03-04"
            m_node = re.search(r"第?\s*(\d+)\s*[-~至到－—]\s*(\d+)\s*节", ln)
            if not m_node:
                m_node = re.search(r"(?:节次|节数)[：:\s]*第?\s*(\d+)\s*[-~至到－—]\s*(\d+)", ln)
            if m_node:
                n1, n2 = int(m_node.group(1)), int(m_node.group(2))
                if 1 <= n1 <= n2 <= 25:
                    start_node = n1
                    step = n2 - n1 + 1
                continue

            # 匹配逗号/顿号分隔节次：如 "第1,2节"、"第3、4、5节"
            m_list_node = re.search(r"第?\s*(\d+(?:\s*[,，、]\s*\d+)+)\s*节", ln)
            if m_list_node:
                nums = [
                    int(x.strip())
                    for x in re.split(r"[,，、]", m_list_node.group(1))
                    if x.strip().isdigit()
                ]
                if nums and 1 <= min(nums) <= max(nums) <= 25:
                    start_node = min(nums)
                    step = max(nums) - min(nums) + 1
                continue

            m_single = re.search(r"第\s*(\d+)\s*节", ln)
            if m_single and start_node is None:
                n1 = int(m_single.group(1))
                if 1 <= n1 <= 25:
                    start_node = n1
                    step = 1
                continue

            m_teacher = re.match(
                r"^(?:教师|老师|任课教师|授课教师|Teacher)[：:\s]+(.+)$", ln, re.I
            )
            if m_teacher:
                teacher = m_teacher.group(1).strip()
                continue
            m_room = re.match(
                r"^(?:教室|地点|上课地点|位置|Location|Room)[：:\s]+(.+)$", ln, re.I
            )
            if m_room and not room:
                room = m_room.group(1).strip()
                continue

        # WakeUp 导出的 .ics DESCRIPTION 常见三行格式：
        # 第1 - 2节 \n 教三201 \n 张老师
        if not teacher and len(desc_lines) >= 2:
            filtered = [
                ln
                for ln in desc_lines
                if not re.search(r"第?\s*\d+.*(?:节|周)", ln) and ln != room
            ]
            if filtered:
                candidate = filtered[-1]
                if len(candidate) <= 20:
                    teacher = candidate

    return (
        course_name,
        room,
        teacher,
        start_node,
        step,
        start_week,
        end_week,
        week_type,
        active_weeks,
    )


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
    max_occurrences = count if count is not None else 200

    if freq == "WEEKLY":
        base_date = start_dt.date()
        base_monday = base_date - timedelta(days=base_date.isoweekday() - 1)
        target_weekdays = sorted(set(bydays)) if bydays else [base_date.isoweekday()]
        week_idx = 0
        while len(dates) < max_occurrences and week_idx < 52:
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
    """
    解析 .ics 日历文本为统一的 ScheduleData：
    - 动态根据课表事件提取每节课的开始时间、结束时间、课程时长与每节课间隙，生成专属 time_slots
    - 动态根据课表事件日期与周数描述确定学期第一周周一与每个人的总周数 (max_week) 及各门课的上课周数
    """
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
            if prop_name == "X-WR-CALNAME" and val_part.strip():
                cal_name = _unescape_ics_text(val_part)
        else:
            current_props.append((prop_name, params, val_part))

    if not raw_events:
        raise ValueError(".ics 文件中未找到任何课程事件 (VEVENT)。")

    # 第一遍扫描：提取所有 VEVENT 的时间、显式节次、显式周数、重复日期
    parsed_events: List[Dict[str, Any]] = []
    explicit_spans: List[Tuple[int, int, int, int]] = []
    unnumbered_intervals: List[Tuple[int, int]] = []

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

        (
            course_name,
            room,
            teacher,
            start_node,
            step,
            exp_start_week,
            exp_end_week,
            exp_week_type,
            exp_active_weeks,
        ) = _extract_meta_from_ics_fields(summary, location, description)

        start_hhmm = dtstart.strftime("%H:%M")
        end_hhmm = dtend.strftime("%H:%M")
        s_min = hhmm_to_minutes(start_hhmm)
        e_min = hhmm_to_minutes(end_hhmm)

        if start_node is not None and step is not None and e_min > s_min:
            explicit_spans.append((start_node, step, s_min, e_min))
        elif e_min > s_min:
            unnumbered_intervals.append((s_min, e_min))

        if rrule_str:
            occ_dates = _expand_rrule_dates(dtstart, rrule_str, exdates, tz_name)
        else:
            d_str = dtstart.date().isoformat()
            occ_dates = [] if d_str in exdates else [dtstart.date()]

        parsed_events.append(
            {
                "course_name": course_name,
                "room": room,
                "teacher": teacher,
                "start_hhmm": start_hhmm,
                "end_hhmm": end_hhmm,
                "start_node": start_node,
                "step": step,
                "exp_start_week": exp_start_week,
                "exp_end_week": exp_end_week,
                "exp_week_type": exp_week_type,
                "exp_active_weeks": exp_active_weeks,
                "has_rrule": bool(rrule_str),
                "occ_dates": occ_dates,
            }
        )

    if not parsed_events:
        raise ValueError("未能从 .ics 文件中解析出有效课程。")

    # 若存在未标注“第X-Y节”的事件，先基于已知事件或全量时间段构建初步动态节次表用于推断节次
    if explicit_spans:
        temp_rules = [
            CourseRule(
                course_name="tmp",
                start_node=sn,
                step=st,
                start_time=minutes_to_hhmm(sm),
                end_time=minutes_to_hhmm(em),
            )
            for sn, st, sm, em in explicit_spans
        ]
        prelim_slots, _ = build_dynamic_time_slots_from_courses(
            temp_rules, force_from_courses=True
        )
        unit_mins, break_mins = infer_unit_and_break_minutes(explicit_spans, prelim_slots)
    else:
        prelim_slots, unit_mins, break_mins = build_slots_for_unnumbered_events(
            unnumbered_intervals
        )

    prelim_slot_dicts = [s.to_dict() for s in prelim_slots]

    # 聚合相同 (课程名, 星期, start_time, end_time, 教室, 教师) 的事件
    grouped_rules: Dict[Tuple[str, int, str, str, str, str], Dict[str, Any]] = {}
    all_dates: List[date] = []
    anchored_mondays: List[date] = []

    for ev in parsed_events:
        start_node = ev["start_node"]
        step = ev["step"]
        if start_node is None or step is None:
            inferred_start, inferred_step = infer_nodes_from_time(
                ev["start_hhmm"],
                ev["end_hhmm"],
                time_slots=prelim_slot_dicts,
                unit_mins=unit_mins,
                break_mins=break_mins,
            )
            start_node = start_node or inferred_start
            step = step or inferred_step

        occ_dates: List[date] = ev["occ_dates"]
        if occ_dates and ev["exp_start_week"] and ev["exp_start_week"] >= 1:
            first_d = min(occ_dates)
            first_mon = first_d - timedelta(days=first_d.isoweekday() - 1)
            anchored_mondays.append(first_mon - timedelta(weeks=ev["exp_start_week"] - 1))

        for occ_d in occ_dates:
            all_dates.append(occ_d)
            wd = occ_d.isoweekday()
            gkey = (
                ev["course_name"],
                wd,
                ev["start_hhmm"],
                ev["end_hhmm"],
                ev["room"],
                ev["teacher"],
            )
            if gkey not in grouped_rules:
                grouped_rules[gkey] = {
                    "course_name": ev["course_name"],
                    "teacher": ev["teacher"],
                    "room": ev["room"],
                    "weekday": wd,
                    "start_node": start_node,
                    "step": step,
                    "start_time": ev["start_hhmm"],
                    "end_time": ev["end_hhmm"],
                    "dates": set(),
                    "exp_start_week": ev["exp_start_week"],
                    "exp_end_week": ev["exp_end_week"],
                    "exp_week_type": ev["exp_week_type"],
                    "exp_active_weeks": list(ev["exp_active_weeks"] or []),
                    "has_rrule": ev["has_rrule"],
                }
            else:
                if ev["exp_active_weeks"]:
                    merged_w = set(grouped_rules[gkey]["exp_active_weeks"]) | set(
                        ev["exp_active_weeks"]
                    )
                    grouped_rules[gkey]["exp_active_weeks"] = sorted(merged_w)
            grouped_rules[gkey]["dates"].add(occ_d.isoformat())

    if not grouped_rules:
        raise ValueError("未能从 .ics 文件中解析出有效课程。")

    earliest_date = min(all_dates) if all_dates else date.today()
    default_monday = earliest_date - timedelta(days=earliest_date.isoweekday() - 1)
    if anchored_mondays:
        semester_monday = min(anchored_mondays)
    else:
        semester_monday = default_monday

    courses: List[CourseRule] = []
    max_observed_week = 1

    for idx, (_, info) in enumerate(grouped_rules.items()):
        sorted_dates = sorted(info["dates"])
        date_week_nums = sorted(
            {
                ((date.fromisoformat(ds) - semester_monday).days // 7) + 1
                for ds in sorted_dates
                if ((date.fromisoformat(ds) - semester_monday).days // 7) + 1 >= 1
            }
        )

        exp_weeks: List[int] = sorted(set(info.get("exp_active_weeks") or []))
        # 若该课程在 ICS 中仅有单个日期（无 RRULE）但 DESCRIPTION 明确声明了多周（如 "1-16周"），则展开到对应周数
        if len(sorted_dates) == 1 and not info["has_rrule"] and len(exp_weeks) > 1:
            active_weeks = exp_weeks
            min_w = min(active_weeks)
            max_w = max(active_weeks)
            week_type = int(info.get("exp_week_type") or 0)
            specific_dates_to_store: List[str] = []
        else:
            active_weeks = date_week_nums or exp_weeks or [1]
            min_w = max(1, min(active_weeks))
            max_w = max(min_w, max(active_weeks))
            week_type = int(info.get("exp_week_type") or 0)
            specific_dates_to_store = sorted_dates

        if max_w > max_observed_week:
            max_observed_week = max_w

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
                week_type=week_type,
                active_weeks=active_weeks,
                specific_dates=specific_dates_to_store,
                color=pick_course_color(info["course_name"], idx),
            )
        )

    dynamic_slots, dynamic_max_nodes = build_dynamic_time_slots_from_courses(
        courses=courses,
        existing_slots=prelim_slots,
        preferred_max_nodes=0,
        force_from_courses=True,
    )

    return ScheduleData(
        source_type="ics",
        table_name=cal_name,
        start_date=semester_monday.isoformat(),
        max_week=max(1, max_observed_week),
        max_nodes=dynamic_max_nodes,
        time_slots=dynamic_slots,
        courses=courses,
        updated_at=now_in_tz(tz_name).strftime("%Y-%m-%d %H:%M:%S"),
    )
