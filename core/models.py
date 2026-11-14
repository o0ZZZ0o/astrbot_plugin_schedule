from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo


DEFAULT_TIME_SLOTS = [
    {"node": 1, "start_time": "08:00", "end_time": "08:45"},
    {"node": 2, "start_time": "08:50", "end_time": "09:35"},
    {"node": 3, "start_time": "09:50", "end_time": "10:35"},
    {"node": 4, "start_time": "10:40", "end_time": "11:25"},
    {"node": 5, "start_time": "11:30", "end_time": "12:15"},
    {"node": 6, "start_time": "14:00", "end_time": "14:45"},
    {"node": 7, "start_time": "14:50", "end_time": "15:35"},
    {"node": 8, "start_time": "15:50", "end_time": "16:35"},
    {"node": 9, "start_time": "16:40", "end_time": "17:25"},
    {"node": 10, "start_time": "18:30", "end_time": "19:15"},
    {"node": 11, "start_time": "19:20", "end_time": "20:05"},
    {"node": 12, "start_time": "20:10", "end_time": "20:55"},
]

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAY_FULL_NAMES = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def get_tz(tz_name: str = "Asia/Shanghai") -> timezone | ZoneInfo:
    """获取时区对象，若系统缺少 tzdata 则回退至 UTC+8"""
    try:
        return ZoneInfo(tz_name or "Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8))


def now_in_tz(tz_name: str = "Asia/Shanghai") -> datetime:
    """获取指定时区的当前时间"""
    return datetime.now(get_tz(tz_name))


def format_duration_cn(minutes: int) -> str:
    """将分钟数格式化为易读的中文时长（如 1小时25分钟 或 35分钟）"""
    minutes = max(0, int(minutes))
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    rem = minutes % 60
    if rem == 0:
        return f"{hours}小时"
    return f"{hours}小时{rem}分钟"


def parse_hhmm(hhmm: str) -> time:
    """解析 HH:MM 字符串为 time 对象"""
    try:
        parts = hhmm.strip().split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return time(max(0, min(23, h)), max(0, min(59, m)))
    except Exception:
        return time(8, 0)


def hhmm_to_minutes(hhmm: str) -> int:
    """将 HH:MM 转换为从 00:00 起的分钟数"""
    t = parse_hhmm(hhmm)
    return t.hour * 60 + t.minute


def minutes_to_hhmm(total_mins: int) -> str:
    """将分钟数转换为 HH:MM 格式"""
    clamped = max(0, min(23 * 60 + 59, int(round(total_mins))))
    return f"{clamped // 60:02d}:{clamped % 60:02d}"


def calc_minutes_between(start_hhmm: str, end_hhmm: str) -> int:
    """计算两个 HH:MM 之间的分钟差"""
    m1 = hhmm_to_minutes(start_hhmm)
    m2 = hhmm_to_minutes(end_hhmm)
    return max(0, m2 - m1)


def infer_unit_and_break_minutes(
    spans: List[Tuple[int, int, int, int]],
    existing_slots: Optional[List["TimeSlot"]] = None,
) -> Tuple[int, int]:
    """
    根据课表中的课程时间跨度 (start_node, step, start_min, end_min) 或已有节次表，
    动态推断该用户课表的单节课时长 (unit_mins) 与课间休息时长 (break_mins)。
    """
    # 1. 若有合法且非默认的 existing_slots，可优先从中统计单节时长与课间间隙
    if existing_slots and len(existing_slots) >= 2:
        unit_cands: List[int] = []
        brk_cands: List[int] = []
        sorted_slots = sorted(existing_slots, key=lambda x: x.node)
        for idx, s in enumerate(sorted_slots):
            st_m = hhmm_to_minutes(s.start_time)
            et_m = hhmm_to_minutes(s.end_time)
            dur = et_m - st_m
            if 25 <= dur <= 90:
                unit_cands.append(dur)
            if idx + 1 < len(sorted_slots) and sorted_slots[idx + 1].node == s.node + 1:
                nxt_st = hhmm_to_minutes(sorted_slots[idx + 1].start_time)
                gap = nxt_st - et_m
                if 0 <= gap <= 25:
                    brk_cands.append(gap)
        if unit_cands and not spans:
            u = Counter(unit_cands).most_common(1)[0][0]
            b = Counter(brk_cands).most_common(1)[0][0] if brk_cands else 10
            return u, b

    # 2. 从课程跨度 (start_node, step, start_min, end_min) 推断
    single_node_durs: List[int] = []
    votes: Counter[Tuple[int, int]] = Counter()

    for _, step, s_min, e_min in spans:
        dur = e_min - s_min
        if dur <= 0:
            continue
        if step == 1 and 25 <= dur <= 100:
            single_node_durs.append(dur)
        elif step >= 2:
            matched_standard = False
            for u_cand in (45, 40, 50, 35, 60):
                rem = dur - step * u_cand
                if rem >= 0 and rem % (step - 1) == 0:
                    b_cand = rem // (step - 1)
                    if 0 <= b_cand <= 25:
                        weight = 3 if (u_cand == 45 and b_cand in (5, 10, 15)) else 2
                        votes[(u_cand, b_cand)] += weight
                        matched_standard = True
            if not matched_standard:
                avg = dur / step
                b_est = 10 if avg >= 48 else (5 if avg >= 42 else 0)
                u_est = max(25, int(round((dur - (step - 1) * b_est) / step)))
                votes[(u_est, b_est)] += 1

    if single_node_durs:
        u_best = Counter(single_node_durs).most_common(1)[0][0]
        # 结合多节课跨度找最佳课间
        brk_votes: Counter[int] = Counter()
        for _, step, s_min, e_min in spans:
            if step >= 2 and e_min > s_min:
                rem = (e_min - s_min) - step * u_best
                b_cand = int(round(rem / (step - 1)))
                if 0 <= b_cand <= 30:
                    brk_votes[b_cand] += 1
        b_best = brk_votes.most_common(1)[0][0] if brk_votes else 10
        return u_best, b_best

    if votes:
        return votes.most_common(1)[0][0]

    return 45, 10


def infer_nodes_from_time(
    start_hhmm: str,
    end_hhmm: str,
    time_slots: Optional[List[Dict[str, Any]]] = None,
    unit_mins: int = 45,
    break_mins: int = 10,
) -> tuple[int, int]:
    """根据上课起止时间与动态节次表推断起始节次 (start_node) 与持续节数 (step)"""
    slots = time_slots or DEFAULT_TIME_SLOTS
    start_min = hhmm_to_minutes(start_hhmm)
    end_min = hhmm_to_minutes(end_hhmm)
    duration = max(unit_mins, end_min - start_min)

    best_start_node = 1
    min_diff = 999999
    for slot in slots:
        st_min = hhmm_to_minutes(str(slot.get("start_time") or slot.get("startTime") or "08:00"))
        diff = abs(st_min - start_min)
        if diff < min_diff:
            min_diff = diff
            best_start_node = int(slot["node"])

    best_end_node = best_start_node
    min_end_diff = 999999
    for slot in slots:
        et_min = hhmm_to_minutes(str(slot.get("end_time") or slot.get("endTime") or "08:45"))
        diff = abs(et_min - end_min)
        if diff < min_end_diff:
            min_end_diff = diff
            best_end_node = int(slot["node"])

    if min_diff <= 35 and min_end_diff <= 35 and best_end_node >= best_start_node:
        step = best_end_node - best_start_node + 1
    else:
        pitch = max(30, unit_mins + break_mins)
        step = max(1, int(round((duration + break_mins) / pitch)))

    return best_start_node, step


@dataclass
class TimeSlot:
    node: int
    start_time: str
    end_time: str

    @property
    def duration_minutes(self) -> int:
        return calc_minutes_between(self.start_time, self.end_time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TimeSlot":
        return cls(
            node=int(data.get("node", 1)),
            start_time=str(data.get("start_time") or data.get("startTime") or "08:00"),
            end_time=str(data.get("end_time") or data.get("endTime") or "08:45"),
        )


def build_dynamic_time_slots_from_courses(
    courses: List["CourseRule"],
    existing_slots: Optional[List[TimeSlot]] = None,
    preferred_max_nodes: int = 0,
    force_from_courses: bool = False,
) -> Tuple[List[TimeSlot], int]:
    """
    根据课程列表的实际开始时间、结束时间、节次跨度与间隙，动态构建匹配的节次时间表 (time_slots) 与最大节数 (max_nodes)。
    - 当课表来自 .ics 或课程实际起止时间与默认节次表不一致时，自动以课表文件中的真实时间锚定每一节的起止时间与课间间隙。
    """
    max_course_node = max(
        (c.start_node + max(1, c.step) - 1 for c in courses), default=0
    )
    if preferred_max_nodes > 0:
        max_nodes = max(preferred_max_nodes, max_course_node)
    elif max_course_node > 0:
        max_nodes = max_course_node
    else:
        max_nodes = 12

    spans: List[Tuple[int, int, int, int]] = []
    for c in courses:
        s_node = max(1, c.start_node)
        step = max(1, c.step)
        s_min = hhmm_to_minutes(c.start_time)
        e_min = hhmm_to_minutes(c.end_time)
        if e_min > s_min:
            spans.append((s_node, step, s_min, e_min))

    unit_mins, break_mins = infer_unit_and_break_minutes(spans, existing_slots)

    # 判断 existing_slots 是否已经与 courses 的起止时间吻合
    slot_map_existing: Dict[int, TimeSlot] = {}
    if existing_slots:
        prev_end = -1
        for s in sorted(existing_slots, key=lambda x: x.node):
            st_m = hhmm_to_minutes(s.start_time)
            et_m = hhmm_to_minutes(s.end_time)
            if 1 <= s.node <= max_nodes and et_m > st_m and st_m >= prev_end:
                slot_map_existing[s.node] = s
                prev_end = et_m

    if not force_from_courses and slot_map_existing and spans:
        mismatch_count = 0
        checked_count = 0
        for s_node, step, s_min, e_min in spans:
            e_node = s_node + step - 1
            if s_node in slot_map_existing and e_node in slot_map_existing:
                checked_count += 1
                st_diff = abs(hhmm_to_minutes(slot_map_existing[s_node].start_time) - s_min)
                et_diff = abs(hhmm_to_minutes(slot_map_existing[e_node].end_time) - e_min)
                if st_diff > 2 or et_diff > 2:
                    mismatch_count += 1
        if checked_count > 0 and mismatch_count == 0 and len(slot_map_existing) >= max_nodes:
            return [slot_map_existing[n] for n in range(1, max_nodes + 1)], max_nodes

    if not spans and slot_map_existing:
        # 补齐缺失节次到 max_nodes
        result_slots: List[TimeSlot] = []
        for n in range(1, max_nodes + 1):
            if n in slot_map_existing:
                result_slots.append(slot_map_existing[n])
            elif result_slots:
                prev_et = hhmm_to_minutes(result_slots[-1].end_time)
                st_m = prev_et + break_mins
                et_m = st_m + unit_mins
                result_slots.append(
                    TimeSlot(node=n, start_time=minutes_to_hhmm(st_m), end_time=minutes_to_hhmm(et_m))
                )
            else:
                def_s = DEFAULT_TIME_SLOTS[min(n - 1, len(DEFAULT_TIME_SLOTS) - 1)]
                result_slots.append(TimeSlot.from_dict({**def_s, "node": n}))
        return result_slots, max_nodes

    # 从课程真实起止时间动态推算每个节次的 start_min 与 end_min
    start_votes: Dict[int, Counter[int]] = {}
    end_votes: Dict[int, Counter[int]] = {}
    for s_node, step, s_min, e_min in spans:
        e_node = s_node + step - 1
        start_votes.setdefault(s_node, Counter())[s_min] += 1
        end_votes.setdefault(e_node, Counter())[e_min] += 1

    node_start: Dict[int, int] = {
        n: cnt.most_common(1)[0][0] for n, cnt in start_votes.items()
    }
    node_end: Dict[int, int] = {
        n: cnt.most_common(1)[0][0] for n, cnt in end_votes.items()
    }

    # 按跨度从小到大排列，先锚定小跨度（如1节、2节），再拆分大跨度（如3节、4节）
    unique_spans = sorted(
        set(spans), key=lambda x: (x[1], x[0], x[2])
    )

    for s_node, step, s_min, e_min in unique_spans:
        e_node = s_node + step - 1
        node_start.setdefault(s_node, s_min)
        node_end.setdefault(e_node, e_min)

        # 尝试利用相邻已知边界推导内部未知节点
        for n in range(s_node, e_node + 1):
            if n > s_node and (n - 1) in node_end and n not in node_start:
                # 若这是最后一节且已知 node_end[n]
                if n == e_node and n in node_end:
                    rem_span = node_end[n] - node_end[n - 1]
                    if rem_span > unit_mins:
                        node_start[n] = node_end[n] - unit_mins
                    else:
                        brk_local = min(break_mins, max(0, rem_span // 5))
                        node_start[n] = node_end[n - 1] + brk_local
                else:
                    node_start[n] = node_end[n - 1] + break_mins
            if n < e_node and (n + 1) in node_start and n not in node_end:
                if n == s_node and n in node_start:
                    rem_span = node_start[n + 1] - node_start[n]
                    if rem_span > unit_mins:
                        node_end[n] = node_start[n] + unit_mins
                    else:
                        brk_local = min(break_mins, max(0, rem_span // 5))
                        node_end[n] = node_start[n + 1] - brk_local
                else:
                    node_end[n] = node_start[n + 1] - break_mins

        # 对 [s_node .. e_node] 内仍未填充的连续子区间进行均匀拆分
        a = s_node
        while a <= e_node:
            if a in node_start and a in node_end and node_end[a] > node_start[a]:
                a += 1
                continue
            b = a
            while b < e_node and not (b in node_end and (b + 1) in node_start):
                b += 1
            st_a = node_start.get(a)
            if st_a is None and (a - 1) in node_end:
                st_a = node_end[a - 1] + break_mins
            if st_a is None:
                st_a = s_min + (a - s_node) * (unit_mins + break_mins)

            et_b = node_end.get(b)
            if et_b is None and (b + 1) in node_start:
                et_b = node_start[b + 1] - break_mins
            if et_b is None:
                et_b = e_min - (e_node - b) * (unit_mins + break_mins)

            m = b - a + 1
            total_m = max(m * 20, et_b - st_a)
            if m == 1:
                node_start[a] = st_a
                node_end[a] = et_b if et_b > st_a else st_a + unit_mins
            else:
                if total_m >= m * unit_mins and (total_m - m * unit_mins) <= 25 * (m - 1):
                    u_local = float(unit_mins)
                    b_local = (total_m - m * unit_mins) / (m - 1)
                elif total_m > (m - 1) * break_mins:
                    b_local = float(break_mins)
                    u_local = (total_m - (m - 1) * break_mins) / m
                else:
                    b_local = 0.0
                    u_local = total_m / m

                for i in range(m):
                    cur_n = a + i
                    c_st = st_a if i == 0 else int(round(st_a + i * (u_local + b_local)))
                    c_et = (
                        et_b
                        if i == m - 1
                        else int(round(st_a + i * (u_local + b_local) + u_local))
                    )
                    node_start.setdefault(cur_n, c_st)
                    node_end.setdefault(cur_n, max(c_st + 15, c_et))
            a = b + 1

    # 补齐 1 .. max_nodes 中完全没有排课的空节次（如午休后空置的第6节或晚间节次）
    default_map = {d["node"]: d for d in DEFAULT_TIME_SLOTS}
    for n in range(1, max_nodes + 1):
        if n in node_start and n not in node_end:
            node_end[n] = node_start[n] + unit_mins
        elif n in node_end and n not in node_start:
            node_start[n] = max(0, node_end[n] - unit_mins)

    for n in range(1, max_nodes + 1):
        if n in node_start and n in node_end:
            continue
        # 若 existing_slots 中有该节次且不与前后已知节次冲突，则优先采用
        prev_et = node_end.get(n - 1, -1)
        next_known_nodes = [k for k in range(n + 1, max_nodes + 1) if k in node_start]
        next_st = node_start[next_known_nodes[0]] if next_known_nodes else 99999
        next_dist = (next_known_nodes[0] - n) if next_known_nodes else 0

        cand_slot = slot_map_existing.get(n)
        if cand_slot:
            c_st = hhmm_to_minutes(cand_slot.start_time)
            c_et = hhmm_to_minutes(cand_slot.end_time)
            if c_st > prev_et and (
                not next_known_nodes
                or c_et + (next_dist - 1) * (unit_mins + break_mins) < next_st
            ):
                node_start[n] = c_st
                node_end[n] = c_et
                continue

        # 若紧邻下一个已知节次（例如第7节已知，第6节未知且第5节与第7节之间有午休大间隔）
        if next_known_nodes and (
            prev_et < 0 or (next_st - prev_et) >= (next_dist + 1) * (unit_mins + break_mins)
        ):
            et_m = next_st - break_mins - (next_dist - 1) * (unit_mins + break_mins)
            st_m = et_m - unit_mins
            if st_m > prev_et:
                node_start[n] = st_m
                node_end[n] = et_m
                continue

        if prev_et >= 0:
            st_m = prev_et + break_mins
            et_m = st_m + unit_mins
            node_start[n] = st_m
            node_end[n] = et_m
        elif n in default_map:
            node_start[n] = hhmm_to_minutes(default_map[n]["start_time"])
            node_end[n] = hhmm_to_minutes(default_map[n]["end_time"])
        else:
            st_m = 8 * 60 + (n - 1) * (unit_mins + break_mins)
            node_start[n] = st_m
            node_end[n] = st_m + unit_mins

    final_slots: List[TimeSlot] = []
    for n in range(1, max_nodes + 1):
        st_m = node_start.get(n, 8 * 60)
        et_m = max(st_m + 15, node_end.get(n, st_m + unit_mins))
        final_slots.append(
            TimeSlot(
                node=n,
                start_time=minutes_to_hhmm(st_m),
                end_time=minutes_to_hhmm(et_m),
            )
        )

    return final_slots, max_nodes


def build_slots_for_unnumbered_events(
    intervals: List[Tuple[int, int]],
) -> Tuple[List[TimeSlot], int, int]:
    """
    当 .ics 文件中完全没有注明“第X-Y节”时，根据所有事件的 (start_min, end_min) 动态推断单节时长、课间休息与节次表。
    返回 (time_slots, unit_mins, break_mins)。
    """
    valid = sorted({(s, e) for s, e in intervals if 0 <= s < e < 1440})
    if not valid:
        return [TimeSlot.from_dict(x) for x in DEFAULT_TIME_SLOTS], 45, 10

    # 估计单节时长与课间
    durs = [e - s for s, e in valid]
    small_durs = [d for d in durs if 25 <= d <= 65]
    if small_durs:
        unit_mins = Counter(small_durs).most_common(1)[0][0]
        break_mins = 10
    else:
        dummy_spans = [(1, max(1, round(d / 50)), s, e) for s, e, d in [(x[0], x[1], x[1] - x[0]) for x in valid]]
        unit_mins, break_mins = infer_unit_and_break_minutes(dummy_spans)

    # 将相近/不重叠的时间段按时间顺序拆解为节次
    clusters: List[Tuple[int, int]] = []
    for s, e in valid:
        if not clusters or s >= clusters[-1][1]:
            clusters.append((s, e))
        else:
            prev_s, prev_e = clusters[-1]
            if e > prev_e:
                clusters[-1] = (prev_s, e)

    slots: List[TimeSlot] = []
    node_idx = 1
    pitch = max(30, unit_mins + break_mins)
    for s, e in clusters:
        dur = e - s
        step = max(1, int(round((dur + break_mins) / pitch)))
        if step == 1:
            slots.append(
                TimeSlot(
                    node=node_idx,
                    start_time=minutes_to_hhmm(s),
                    end_time=minutes_to_hhmm(e),
                )
            )
            node_idx += 1
        else:
            b_local = (dur - step * unit_mins) / (step - 1) if dur >= step * unit_mins else float(break_mins)
            b_local = max(0.0, min(25.0, b_local))
            u_local = (dur - (step - 1) * b_local) / step
            for i in range(step):
                c_st = s if i == 0 else int(round(s + i * (u_local + b_local)))
                c_et = e if i == step - 1 else int(round(s + i * (u_local + b_local) + u_local))
                slots.append(
                    TimeSlot(
                        node=node_idx,
                        start_time=minutes_to_hhmm(c_st),
                        end_time=minutes_to_hhmm(max(c_st + 15, c_et)),
                    )
                )
                node_idx += 1

    return slots, unit_mins, break_mins


def format_weeks_cn(
    start_week: int,
    end_week: int,
    week_type: int = 0,
    active_weeks: Optional[List[int]] = None,
) -> str:
    """将课程周数范围格式化为易读文本（如 1-16周、1-16周(单)、1-8,10-16周）"""
    if active_weeks:
        w_sorted = sorted(set(int(w) for w in active_weeks if int(w) >= 1))
        if len(w_sorted) == 1:
            return f"第{w_sorted[0]}周"
        if w_sorted:
            w_min, w_max = w_sorted[0], w_sorted[-1]
            if w_sorted == list(range(w_min, w_max + 1)):
                return f"{w_min}-{w_max}周"
            if len(w_sorted) >= 2 and w_sorted == [w for w in range(w_min, w_max + 1) if w % 2 == 1]:
                return f"{w_min}-{w_max}周(单)"
            if len(w_sorted) >= 2 and w_sorted == [w for w in range(w_min, w_max + 1) if w % 2 == 0]:
                return f"{w_min}-{w_max}周(双)"
            # 合并连续区间
            ranges: List[str] = []
            seg_start = seg_end = w_sorted[0]
            for w in w_sorted[1:]:
                if w == seg_end + 1:
                    seg_end = w
                else:
                    ranges.append(f"{seg_start}" if seg_start == seg_end else f"{seg_start}-{seg_end}")
                    seg_start = seg_end = w
            ranges.append(f"{seg_start}" if seg_start == seg_end else f"{seg_start}-{seg_end}")
            return ",".join(ranges) + "周"

    if start_week == end_week:
        return f"第{start_week}周"
    suffix = "(单)" if week_type == 1 else ("(双)" if week_type == 2 else "")
    return f"{start_week}-{end_week}周{suffix}"


@dataclass
class CourseRule:
    course_name: str
    teacher: str = ""
    room: str = ""
    weekday: int = 1  # 1=周一 ... 7=周日
    start_node: int = 1
    step: int = 2
    start_time: str = "08:00"
    end_time: str = "09:35"
    start_week: int = 1
    end_week: int = 20
    week_type: int = 0  # 0=每周, 1=单周, 2=双周
    active_weeks: List[int] = field(default_factory=list)  # 非连续周列表（如 [1,2,3,6,7,8]）
    specific_dates: List[str] = field(default_factory=list)  # YYYY-MM-DD
    excluded_dates: List[str] = field(default_factory=list)  # YYYY-MM-DD
    color: str = ""

    @property
    def week_range_str(self) -> str:
        return format_weeks_cn(
            self.start_week, self.end_week, self.week_type, self.active_weeks
        )

    def matches_date(
        self, target_date: date, week_num: int, max_week: int = 0
    ) -> bool:
        date_str = target_date.isoformat()
        if date_str in self.excluded_dates:
            return False
        if self.specific_dates:
            return date_str in self.specific_dates
        if target_date.isoweekday() != self.weekday:
            return False
        if week_num < 1:
            return False
        if max_week > 0 and week_num > max_week:
            return False
        if self.active_weeks:
            return week_num in self.active_weeks
        if week_num < self.start_week or week_num > self.end_week:
            return False
        if self.week_type == 1 and week_num % 2 == 0:
            return False
        if self.week_type == 2 and week_num % 2 == 1:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CourseRule":
        return cls(
            course_name=str(data.get("course_name", "未命名课程")),
            teacher=str(data.get("teacher", "")),
            room=str(data.get("room", "")),
            weekday=int(data.get("weekday", 1)),
            start_node=int(data.get("start_node", 1)),
            step=max(1, int(data.get("step", 1))),
            start_time=str(data.get("start_time", "08:00")),
            end_time=str(data.get("end_time", "09:35")),
            start_week=int(data.get("start_week", 1)),
            end_week=int(data.get("end_week", 20)),
            week_type=int(data.get("week_type", 0)),
            active_weeks=[int(w) for w in data.get("active_weeks", []) if str(w).lstrip("-").isdigit()],
            specific_dates=list(data.get("specific_dates", [])),
            excluded_dates=list(data.get("excluded_dates", [])),
            color=str(data.get("color", "")),
        )


@dataclass
class CourseInstance:
    course_name: str
    teacher: str
    room: str
    date_str: str
    weekday: int
    week_num: int
    start_node: int
    end_node: int
    step: int
    start_time: str
    end_time: str
    start_dt: datetime
    end_dt: datetime
    duration_minutes: int
    week_range_str: str = ""
    status: str = "upcoming"  # ongoing | next | upcoming | finished
    status_text: str = "待开始"
    wait_or_remain_minutes: int = 0
    progress: float = 0.0
    color: str = ""

    @property
    def node_range_str(self) -> str:
        if self.start_node == self.end_node:
            return f"第{self.start_node}节"
        return f"第{self.start_node}-{self.end_node}节"

    @property
    def unique_key(self) -> str:
        return f"{self.date_str}_{self.start_time}_{self.course_name}"


@dataclass
class ScheduleData:
    source_type: str = "wakeup"  # wakeup | ics
    table_name: str = "我的课表"
    start_date: str = "2026-09-07"  # 第一周周一的日期 YYYY-MM-DD
    max_week: int = 20
    max_nodes: int = 12
    time_slots: List[TimeSlot] = field(
        default_factory=lambda: [TimeSlot.from_dict(x) for x in DEFAULT_TIME_SLOTS]
    )
    courses: List[CourseRule] = field(default_factory=list)
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.reconcile_dynamic_schedule()

    def reconcile_dynamic_schedule(self) -> None:
        """
        根据课表中的课程数据动态校准：
        1. 每个节次的开始时间、结束时间、单节时长、每节课间隙 (time_slots) 及总节数 (max_nodes)
        2. 每个课程的上课周数 (active_weeks / start_week / end_week) 及个人总周数 (max_week)
        """
        if not self.courses:
            return

        # 1. 动态校准课程周数与总周数 max_week
        semester_monday = self.get_semester_monday()
        for c in self.courses:
            if c.specific_dates:
                w_nums: List[int] = []
                for ds in c.specific_dates:
                    try:
                        d_obj = date.fromisoformat(ds)
                        wn = ((d_obj - semester_monday).days // 7) + 1
                        if wn >= 1:
                            w_nums.append(wn)
                    except Exception:
                        pass
                if w_nums:
                    c.active_weeks = sorted(set(w_nums))
                    c.start_week = min(c.active_weeks)
                    c.end_week = max(c.active_weeks)
            elif c.active_weeks:
                c.start_week = min(c.active_weeks)
                c.end_week = max(c.active_weeks)

        # 修复旧版 .ics 因 UNTIL=SundayT235959Z 转换 UTC+8 溢出到下周一 07:59:59，
        # 导致仅周一课程比周二至周日课程多出 1 周（如 18 周课表变成 19 周）的问题
        if self.source_type == "ics" and len(self.courses) >= 2:
            monday_courses = [c for c in self.courses if c.weekday == 1]
            other_courses = [c for c in self.courses if c.weekday != 1]
            if monday_courses and other_courses:
                max_other_w = max(c.end_week for c in other_courses)
                max_mon_w = max(c.end_week for c in monday_courses)
                if max_other_w >= 4 and max_mon_w == max_other_w + 1:
                    for mc in monday_courses:
                        if mc.end_week == max_mon_w and len(mc.specific_dates) >= 2:
                            mc.specific_dates = [
                                ds
                                for ds in mc.specific_dates
                                if (((date.fromisoformat(ds) - semester_monday).days // 7) + 1)
                                <= max_other_w
                            ]
                            mc.active_weeks = [w for w in mc.active_weeks if w <= max_other_w]
                            if mc.active_weeks:
                                mc.end_week = max(mc.active_weeks)
                    if self.max_week == max_mon_w:
                        self.max_week = max_other_w

        computed_max_week = max((c.end_week for c in self.courses), default=0)
        if computed_max_week > 0:
            if self.source_type == "ics":
                self.max_week = computed_max_week
            elif computed_max_week > self.max_week:
                self.max_week = computed_max_week

        # 2. 动态校准 time_slots 与 max_nodes
        force_from_courses = self.source_type == "ics"
        pref_nodes = 0 if self.source_type == "ics" else self.max_nodes
        new_slots, new_max_nodes = build_dynamic_time_slots_from_courses(
            courses=self.courses,
            existing_slots=self.time_slots,
            preferred_max_nodes=pref_nodes,
            force_from_courses=force_from_courses,
        )
        self.time_slots = new_slots
        self.max_nodes = new_max_nodes

    def set_current_week(self, target_week: int, reference_date: Optional[date] = None) -> None:
        """
        动态调整用户当前所在周数（自动平移学期第一周周一 start_date，并同步修正基于具体日期的课程周数）
        """
        target_week = max(1, int(target_week))
        ref_d = reference_date or date.today()
        ref_monday = ref_d - timedelta(days=ref_d.isoweekday() - 1)
        old_monday = self.get_semester_monday()
        new_monday = ref_monday - timedelta(weeks=target_week - 1)
        week_shift = (old_monday - new_monday).days // 7

        self.start_date = new_monday.isoformat()
        if week_shift != 0:
            for c in self.courses:
                if c.specific_dates:
                    w_nums = []
                    for ds in c.specific_dates:
                        try:
                            d_obj = date.fromisoformat(ds)
                            wn = ((d_obj - new_monday).days // 7) + 1
                            if wn >= 1:
                                w_nums.append(wn)
                        except Exception:
                            pass
                    if w_nums:
                        c.active_weeks = sorted(set(w_nums))
                        c.start_week = min(c.active_weeks)
                        c.end_week = max(c.active_weeks)

        max_c_week = max((c.end_week for c in self.courses), default=target_week)
        if max_c_week > self.max_week or target_week > self.max_week:
            self.max_week = max(self.max_week, max_c_week, target_week)

    def get_semester_monday(self) -> date:
        try:
            parts = [int(p) for p in self.start_date.strip().split("-")]
            d = date(parts[0], parts[1], parts[2])
            return d - timedelta(days=d.isoweekday() - 1)
        except Exception:
            today = date.today()
            return today - timedelta(days=today.isoweekday() - 1)

    def get_raw_week_number(self, target_date: date) -> int:
        """获取目标日期相对于第一周周一的真实周序号（未开学时 <= 0）"""
        monday = self.get_semester_monday()
        delta_days = (target_date - monday).days
        return (delta_days // 7) + 1

    def get_week_number(self, target_date: date) -> int:
        """获取用于展示的周数（最小为 1）"""
        raw = self.get_raw_week_number(target_date)
        return max(1, raw)

    def get_courses_for_date(
        self,
        target_date: date,
        now: Optional[datetime] = None,
        tz_name: str = "Asia/Shanghai",
    ) -> List[CourseInstance]:
        tz = get_tz(tz_name)
        if now is None:
            now = now_in_tz(tz_name)
        else:
            now = now.astimezone(tz)

        raw_week_num = self.get_raw_week_number(target_date)
        display_week_num = max(1, raw_week_num)
        slot_map = {s.node: s for s in self.time_slots}
        unit_mins, break_mins = infer_unit_and_break_minutes([], self.time_slots)

        instances: List[CourseInstance] = []
        seen_keys = set()

        for rule in self.courses:
            if not rule.matches_date(target_date, raw_week_num, self.max_week):
                continue

            step = max(1, rule.step)
            end_node = rule.start_node + step - 1

            # 优先使用课程规则自身的起止时间，若缺失或异常则根据动态节次表计算
            st_str = rule.start_time or (
                slot_map[rule.start_node].start_time
                if rule.start_node in slot_map
                else "08:00"
            )
            et_str = rule.end_time or (
                slot_map[end_node].end_time if end_node in slot_map else ""
            )

            t_start = parse_hhmm(st_str)
            start_dt = datetime.combine(target_date, t_start, tzinfo=tz)

            if et_str:
                t_end = parse_hhmm(et_str)
                end_dt = datetime.combine(target_date, t_end, tzinfo=tz)
            else:
                end_dt = start_dt

            if end_dt <= start_dt:
                if end_node in slot_map:
                    t_end_slot = parse_hhmm(slot_map[end_node].end_time)
                    end_dt = datetime.combine(target_date, t_end_slot, tzinfo=tz)
                if end_dt <= start_dt:
                    est_mins = step * unit_mins + max(0, step - 1) * break_mins
                    end_dt = start_dt + timedelta(minutes=max(unit_mins, est_mins))
                et_str = end_dt.strftime("%H:%M")

            dedup_key = (
                rule.course_name,
                st_str,
                et_str,
                rule.start_node,
                rule.room,
            )
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            duration_minutes = max(1, int((end_dt - start_dt).total_seconds() // 60))

            inst = CourseInstance(
                course_name=rule.course_name,
                teacher=rule.teacher,
                room=rule.room,
                date_str=target_date.isoformat(),
                weekday=target_date.isoweekday(),
                week_num=display_week_num,
                start_node=rule.start_node,
                end_node=end_node,
                step=step,
                start_time=st_str,
                end_time=et_str,
                start_dt=start_dt,
                end_dt=end_dt,
                duration_minutes=duration_minutes,
                week_range_str=rule.week_range_str,
                color=rule.color,
            )
            instances.append(inst)

        instances.sort(key=lambda x: (x.start_dt, x.start_node, x.course_name))
        self._annotate_status(instances, target_date, now)
        return instances

    @staticmethod
    def _annotate_status(
        instances: List[CourseInstance], target_date: date, now: datetime
    ) -> None:
        today = now.date()
        next_start_dt: Optional[datetime] = None

        if target_date == today:
            for inst in instances:
                if inst.start_dt > now:
                    next_start_dt = inst.start_dt
                    break

        for inst in instances:
            if target_date < today:
                inst.status = "finished"
                inst.status_text = "已结束"
                inst.progress = 1.0
            elif target_date > today:
                inst.status = "upcoming"
                wait_mins = max(
                    1, math.ceil((inst.start_dt - now).total_seconds() / 60)
                )
                inst.wait_or_remain_minutes = wait_mins
                inst.status_text = f"{inst.start_time} 开始"
                inst.progress = 0.0
            else:
                if inst.end_dt <= now:
                    inst.status = "finished"
                    inst.status_text = "已结束"
                    inst.wait_or_remain_minutes = 0
                    inst.progress = 1.0
                elif inst.start_dt <= now < inst.end_dt:
                    inst.status = "ongoing"
                    remain_mins = max(
                        1, math.ceil((inst.end_dt - now).total_seconds() / 60)
                    )
                    total_secs = max(1.0, (inst.end_dt - inst.start_dt).total_seconds())
                    elapsed_secs = max(0.0, (now - inst.start_dt).total_seconds())
                    inst.progress = min(1.0, max(0.05, elapsed_secs / total_secs))
                    inst.wait_or_remain_minutes = remain_mins
                    inst.status_text = f"进行中 · 剩余 {format_duration_cn(remain_mins)}"
                else:
                    wait_mins = max(
                        1, math.ceil((inst.start_dt - now).total_seconds() / 60)
                    )
                    inst.wait_or_remain_minutes = wait_mins
                    inst.progress = 0.0
                    if next_start_dt is not None and inst.start_dt == next_start_dt:
                        inst.status = "next"
                        inst.status_text = (
                            f"下一节 · {format_duration_cn(wait_mins)}后开始"
                        )
                    else:
                        inst.status = "upcoming"
                        inst.status_text = (
                            f"待开始 · {format_duration_cn(wait_mins)}后"
                        )

    def get_courses_for_week(
        self,
        monday_date: date,
        now: Optional[datetime] = None,
        tz_name: str = "Asia/Shanghai",
    ) -> Dict[int, List[CourseInstance]]:
        monday = monday_date - timedelta(days=monday_date.isoweekday() - 1)
        result: Dict[int, List[CourseInstance]] = {}
        for i in range(7):
            day_date = monday + timedelta(days=i)
            result[i + 1] = self.get_courses_for_date(day_date, now=now, tz_name=tz_name)
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "table_name": self.table_name,
            "start_date": self.start_date,
            "max_week": self.max_week,
            "max_nodes": self.max_nodes,
            "time_slots": [s.to_dict() for s in self.time_slots],
            "courses": [c.to_dict() for c in self.courses],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduleData":
        raw_slots = data.get("time_slots") or DEFAULT_TIME_SLOTS
        raw_courses = data.get("courses") or []
        return cls(
            source_type=str(data.get("source_type", "wakeup")),
            table_name=str(data.get("table_name", "我的课表")),
            start_date=str(data.get("start_date", "2026-09-07")),
            max_week=int(data.get("max_week", 20)),
            max_nodes=int(data.get("max_nodes", 12)),
            time_slots=[TimeSlot.from_dict(s) for s in raw_slots],
            courses=[CourseRule.from_dict(c) for c in raw_courses],
            updated_at=str(data.get("updated_at", "")),
        )


@dataclass
class UserProfile:
    user_id: str  # 平台唯一标识（QQ官方机器人为 openid，OneBot 为 QQ号）
    qq_number: str = ""  # 可选绑定的真实数字 QQ 号（用于拉取高清QQ头像和QQ昵称）
    nickname: str = ""  # 默认展示昵称
    avatar_url: str = ""  # 缓存的头像 URL
    groups: List[str] = field(default_factory=list)  # 所在群聊 ID 列表
    group_nicknames: Dict[str, str] = field(default_factory=dict)  # group_id -> 群昵称
    user_umo: str = ""  # 最近一次交互的 UMO
    group_umos: Dict[str, str] = field(default_factory=dict)  # group_id -> UMO
    daily_push_enabled: bool = False
    daily_push_time: str = "07:30"
    daily_push_umo: str = ""
    reminder_enabled: bool = True  # 课前提醒独立开关（默认开启）
    reminder_minutes: int = 15  # 提前提醒分钟数 (1-120)
    reminder_umo: str = ""
    last_daily_push_date: str = ""
    reminded_classes: List[str] = field(default_factory=list)
    # 早八提醒配置（在有早八的前一天指定时间推送并 @用户）
    early_eight_enabled: bool = True  # 默认开启
    early_eight_remind_time: str = "21:30"  # 前一天几点提醒，如 21:30
    early_eight_threshold: str = "08:30"  # 几点及之前开始的课算早八，默认 08:30
    early_eight_umo: str = ""
    last_early_eight_remind_date: str = ""
    # 周日推送下周上课情况配置
    sunday_push_enabled: bool = True  # 默认开启
    sunday_push_time: str = "20:00"  # 周日几点推送下周上课情况，如 20:00
    sunday_push_umo: str = ""
    sunday_push_group_id: str = ""
    last_sunday_push_date: str = ""

    def get_display_name(self, group_id: Optional[str] = None) -> str:
        """优先返回群昵称，绝不直接暴露冗长的 OpenID 或纯 QQ 号"""
        if group_id and group_id in self.group_nicknames:
            gn = self.group_nicknames[group_id].strip()
            if gn and not self._is_raw_id(gn):
                return gn
        if self.nickname and not self._is_raw_id(self.nickname):
            return self.nickname.strip()
        if self.qq_number:
            return f"群友({self.qq_number[-4:]})"
        short_id = self.user_id[-4:] if len(self.user_id) >= 4 else self.user_id
        return f"群友#{short_id}"

    def _is_raw_id(self, name: str) -> bool:
        """判断名称是否只是原始 OpenID 或纯数字 QQ 号"""
        name = name.strip()
        if not name or name.lower() == "unknown":
            return True
        if name == self.user_id:
            return True
        if len(name) >= 24 and all(c in "0123456789abcdefABCDEF-_" for c in name):
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserProfile":
        raw_rem_mins = int(data.get("reminder_minutes", 15))
        if "reminder_enabled" in data:
            rem_enabled = bool(data.get("reminder_enabled", True))
        else:
            rem_enabled = raw_rem_mins > 0
        if raw_rem_mins <= 0:
            raw_rem_mins = 15

        return cls(
            user_id=str(data.get("user_id", "")),
            qq_number=str(data.get("qq_number", "")),
            nickname=str(data.get("nickname", "")),
            avatar_url=str(data.get("avatar_url", "")),
            groups=list(data.get("groups", [])),
            group_nicknames=dict(data.get("group_nicknames", {})),
            user_umo=str(data.get("user_umo", "")),
            group_umos=dict(data.get("group_umos", {})),
            daily_push_enabled=bool(data.get("daily_push_enabled", False)),
            daily_push_time=str(data.get("daily_push_time", "07:30")),
            daily_push_umo=str(data.get("daily_push_umo", "")),
            reminder_enabled=rem_enabled,
            reminder_minutes=raw_rem_mins,
            reminder_umo=str(data.get("reminder_umo", "")),
            last_daily_push_date=str(data.get("last_daily_push_date", "")),
            reminded_classes=list(data.get("reminded_classes", [])),
            early_eight_enabled=bool(data.get("early_eight_enabled", True)),
            early_eight_remind_time=str(data.get("early_eight_remind_time", "21:30")),
            early_eight_threshold=str(data.get("early_eight_threshold", "08:30")),
            early_eight_umo=str(data.get("early_eight_umo", "")),
            last_early_eight_remind_date=str(
                data.get("last_early_eight_remind_date", "")
            ),
            sunday_push_enabled=bool(data.get("sunday_push_enabled", True)),
            sunday_push_time=str(data.get("sunday_push_time", "20:00")),
            sunday_push_umo=str(data.get("sunday_push_umo", "")),
            sunday_push_group_id=str(data.get("sunday_push_group_id", "")),
            last_sunday_push_date=str(data.get("last_sunday_push_date", "")),
        )
