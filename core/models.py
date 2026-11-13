from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
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
    parts = hhmm.strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return time(max(0, min(23, h)), max(0, min(59, m)))


def calc_minutes_between(start_hhmm: str, end_hhmm: str) -> int:
    """计算两个 HH:MM 之间的分钟差"""
    t1 = parse_hhmm(start_hhmm)
    t2 = parse_hhmm(end_hhmm)
    m1 = t1.hour * 60 + t1.minute
    m2 = t2.hour * 60 + t2.minute
    return max(0, m2 - m1)


def infer_nodes_from_time(
    start_hhmm: str,
    end_hhmm: str,
    time_slots: Optional[List[Dict[str, Any]]] = None,
) -> tuple[int, int]:
    """根据上课起止时间推断起始节次 (start_node) 与持续节数 (step)"""
    slots = time_slots or DEFAULT_TIME_SLOTS
    t_start = parse_hhmm(start_hhmm)
    t_end = parse_hhmm(end_hhmm)
    start_min = t_start.hour * 60 + t_start.minute
    end_min = t_end.hour * 60 + t_end.minute
    duration = max(45, end_min - start_min)

    best_start_node = 1
    min_diff = 999999
    for slot in slots:
        st = parse_hhmm(slot["start_time"])
        st_min = st.hour * 60 + st.minute
        diff = abs(st_min - start_min)
        if diff < min_diff:
            min_diff = diff
            best_start_node = int(slot["node"])

    best_end_node = best_start_node
    min_end_diff = 999999
    for slot in slots:
        et = parse_hhmm(slot["end_time"])
        et_min = et.hour * 60 + et.minute
        diff = abs(et_min - end_min)
        if diff < min_end_diff:
            min_end_diff = diff
            best_end_node = int(slot["node"])

    if min_diff <= 35 and min_end_diff <= 35 and best_end_node >= best_start_node:
        step = best_end_node - best_start_node + 1
    else:
        step = max(1, round(duration / 45))

    return best_start_node, step


@dataclass
class TimeSlot:
    node: int
    start_time: str
    end_time: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TimeSlot":
        return cls(
            node=int(data.get("node", 1)),
            start_time=str(data.get("start_time") or data.get("startTime") or "08:00"),
            end_time=str(data.get("end_time") or data.get("endTime") or "08:45"),
        )


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
    specific_dates: List[str] = field(default_factory=list)  # YYYY-MM-DD
    excluded_dates: List[str] = field(default_factory=list)  # YYYY-MM-DD
    color: str = ""

    def matches_date(self, target_date: date, week_num: int) -> bool:
        date_str = target_date.isoformat()
        if date_str in self.excluded_dates:
            return False
        if self.specific_dates:
            return date_str in self.specific_dates
        if target_date.isoweekday() != self.weekday:
            return False
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

    def get_semester_monday(self) -> date:
        try:
            parts = [int(p) for p in self.start_date.strip().split("-")]
            d = date(parts[0], parts[1], parts[2])
            # 归一到所在周的周一
            return d - timedelta(days=d.isoweekday() - 1)
        except Exception:
            today = date.today()
            return today - timedelta(days=today.isoweekday() - 1)

    def get_week_number(self, target_date: date) -> int:
        monday = self.get_semester_monday()
        delta_days = (target_date - monday).days
        if delta_days < 0:
            return 1
        return (delta_days // 7) + 1

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

        week_num = self.get_week_number(target_date)
        instances: List[CourseInstance] = []
        seen_keys = set()

        for rule in self.courses:
            if not rule.matches_date(target_date, week_num):
                continue
            t_start = parse_hhmm(rule.start_time)
            t_end = parse_hhmm(rule.end_time)
            start_dt = datetime.combine(target_date, t_start, tzinfo=tz)
            end_dt = datetime.combine(target_date, t_end, tzinfo=tz)
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(minutes=45 * max(1, rule.step))

            dedup_key = (
                rule.course_name,
                rule.start_time,
                rule.end_time,
                rule.start_node,
                rule.room,
            )
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            duration_minutes = max(1, int((end_dt - start_dt).total_seconds() // 60))
            end_node = rule.start_node + max(1, rule.step) - 1

            inst = CourseInstance(
                course_name=rule.course_name,
                teacher=rule.teacher,
                room=rule.room,
                date_str=target_date.isoformat(),
                weekday=target_date.isoweekday(),
                week_num=week_num,
                start_node=rule.start_node,
                end_node=end_node,
                step=max(1, rule.step),
                start_time=rule.start_time,
                end_time=rule.end_time,
                start_dt=start_dt,
                end_dt=end_dt,
                duration_minutes=duration_minutes,
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
        next_Start_dt: Optional[datetime] = None

        if target_date == today:
            # 找出今天第一批尚未开始的课程时间作为“下一节”
            for inst in instances:
                if inst.start_dt > now:
                    next_Start_dt = inst.start_dt
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
                # 今天
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
                    if next_Start_dt is not None and inst.start_dt == next_Start_dt:
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
    reminder_minutes: int = 0  # 0 表示未开启，1-120 表示提前分钟数
    reminder_umo: str = ""
    last_daily_push_date: str = ""
    reminded_classes: List[str] = field(default_factory=list)

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
        # 32位或更长的 hex/UUID 格式 OpenID
        if len(name) >= 24 and all(c in "0123456789abcdefABCDEF-_" for c in name):
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserProfile":
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
            reminder_minutes=int(data.get("reminder_minutes", 0)),
            reminder_umo=str(data.get("reminder_umo", "")),
            last_daily_push_date=str(data.get("last_daily_push_date", "")),
            reminded_classes=list(data.get("reminded_classes", [])),
        )
