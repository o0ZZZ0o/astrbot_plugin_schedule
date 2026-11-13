from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.models import get_tz
from core.parser import (
    extract_wakeup_key,
    parse_ics_content,
    parse_wakeup_raw_data,
)
from core.qq_official import (
    build_candidate_avatar_urls,
    extract_event_identity,
    extract_qq_official_appid,
)
from main import SchedulePlugin


SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//WakeUp Schedule//CN
X-WR-CALNAME:2026秋季学期课表
BEGIN:VEVENT
DTSTART;TZID=Asia/Shanghai:20261109T080000
DTEND;TZID=Asia/Shanghai:20261109T093500
RRULE:FREQ=WEEKLY;COUNT=16
SUMMARY:高等数学(上)
LOCATION:理教201
DESCRIPTION:第1 - 2节\\n理教201\\n王教授
END:VEVENT
BEGIN:VEVENT
DTSTART:20261113T020000Z
DTEND:20261113T033500Z
SUMMARY:大学物理实验
LOCATION:物理楼305
DESCRIPTION:第3-4节\\n教师: 李老师
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Shanghai:20261113T140000
DTEND;TZID=Asia/Shanghai:20261113T153500
SUMMARY:计算机网络
LOCATION:信息楼402
DESCRIPTION:第6 - 7节\\n信息楼402\\n赵老师
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Shanghai:20261114T080000
DTEND;TZID=Asia/Shanghai:20261114T093500
SUMMARY:程序设计实践
LOCATION:机房B102
DESCRIPTION:第1 - 2节\\n机房B102\\n陈老师
END:VEVENT
END:VCALENDAR
"""

SAMPLE_WAKEUP_5LINES = "\n".join(
    [
        json.dumps({"courseLen": 45, "theBreakLen": 5, "sameLen": True}),
        json.dumps(
            [
                {"node": 1, "startTime": "08:00", "endTime": "08:45", "timeTable": 1},
                {"node": 2, "startTime": "08:50", "endTime": "09:35", "timeTable": 1},
                {"node": 3, "startTime": "09:50", "endTime": "10:35", "timeTable": 1},
                {"node": 4, "startTime": "10:40", "endTime": "11:25", "timeTable": 1},
                {"node": 5, "startTime": "11:30", "endTime": "12:15", "timeTable": 1},
                {"node": 6, "startTime": "14:00", "endTime": "14:45", "timeTable": 1},
                {"node": 7, "startTime": "14:50", "endTime": "15:35", "timeTable": 1},
                {"node": 8, "startTime": "15:50", "endTime": "16:35", "timeTable": 1},
            ]
        ),
        json.dumps(
            {
                "startDate": "2026-09-07",
                "maxWeek": 20,
                "nodes": 12,
                "tableName": "WakeUp测试课表",
                "sundayFirst": False,
            }
        ),
        json.dumps(
            [
                {"id": 0, "courseName": "数据结构与算法", "color": "#4F46E5"},
                {"id": 1, "courseName": "操作系统原理", "color": "#0D9488"},
            ]
        ),
        json.dumps(
            [
                {
                    "id": 0,
                    "day": 5,
                    "startNode": 1,
                    "step": 2,
                    "startWeek": 1,
                    "endWeek": 18,
                    "type": 0,
                    "room": "科教楼101",
                    "teacher": "林教授",
                },
                {
                    "id": 1,
                    "day": 5,
                    "startNode": 6,
                    "step": 3,
                    "startWeek": 1,
                    "endWeek": 18,
                    "type": 0,
                    "room": "科教楼302",
                    "teacher": "周教授",
                },
                {
                    "id": 0,
                    "day": 6,
                    "startNode": 3,
                    "step": 2,
                    "startWeek": 1,
                    "endWeek": 18,
                    "type": 0,
                    "room": "实验楼204",
                    "teacher": "林教授",
                },
            ]
        ),
    ]
)


class FakeContext:
    def __init__(self, appid: str = "102030405"):
        self.sent_messages: List[Dict[str, Any]] = []
        self.platform_manager = type(
            "PM",
            (),
            {
                "platform_insts": [
                    type("Inst", (), {"config": {"appid": appid}})()
                ]
            },
        )()

    async def send_message(self, umo: str, chain: Any) -> None:
        self.sent_messages.append({"umo": umo, "chain": chain})


class FakeQQOfficialEvent:
    """模拟 AstrBot 下 QQ 官方机器人 (qq_official) 消息事件"""

    def __init__(
        self,
        message_str: str,
        member_openid: str = "9F8E7D6C5B4A32109F8E7D6C5B4A3210",
        group_openid: str = "GROUP_OPENID_888888",
        sender_name: str = "",
        attachments: Optional[List[Dict[str, str]]] = None,
    ):
        self.message_str = message_str
        self._sender_id = member_openid
        self._group_id = group_openid
        self._sender_name = sender_name
        self.unified_msg_origin = (
            f"qq_official:GroupMessage:{member_openid}_{group_openid}"
            if group_openid
            else f"qq_official:FriendMessage:{member_openid}"
        )
        raw_msg = {
            "group_openid": group_openid,
            "author": {
                "member_openid": member_openid,
                "id": member_openid,
            },
            "attachments": attachments or [],
        }
        if sender_name:
            raw_msg["member"] = {"nick": sender_name}
        self.message_obj = type(
            "MsgObj",
            (),
            {
                "group_id": group_openid,
                "sender": type("Sender", (), {"nickname": sender_name, "card": ""})(),
                "raw_message": raw_msg,
                "message": [],
            },
        )()
        self.bot = type("Bot", (), {"appid": "102030405"})()

    def get_platform_name(self) -> str:
        return "qq_official"

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_name(self) -> str:
        return self._sender_name or self._sender_id

    def plain_result(self, text: str) -> Dict[str, str]:
        return {"type": "plain", "text": text}

    def image_result(self, path: str) -> Dict[str, str]:
        return {"type": "image", "path": path}

    def stop_event(self) -> None:
        pass


async def collect_gen(agen) -> List[Dict[str, str]]:
    out = []
    async for item in agen:
        out.append(item)
    return out


class TestSchedulePlugin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tmp_dir.name)
        self.ctx = FakeContext(appid="102099999")
        self.plugin = SchedulePlugin(
            context=self.ctx,
            config={"timezone": "Asia/Shanghai", "qq_appid": "102099999"},
            data_dir=self.data_path,
        )

    async def asyncTearDown(self) -> None:
        await self.plugin.terminate()
        self.tmp_dir.cleanup()

    def test_wakeup_key_extraction_and_parsing(self) -> None:
        share_msg = (
            "这是来自「WakeUp课程表」的课表分享，30分钟内有效哦，如果失效请朋友再分享一次叭。"
            "请复制这条消息后打开App导入~分享口令为「a1b2c3d4e5f60718293a4b5c6d7e8f00」"
        )
        key = extract_wakeup_key(share_msg)
        self.assertEqual(key, "a1b2c3d4e5f60718293a4b5c6d7e8f00")

        sched = parse_wakeup_raw_data(SAMPLE_WAKEUP_5LINES)
        self.assertEqual(sched.table_name, "WakeUp测试课表")
        self.assertEqual(len(sched.courses), 3)

        # 2026-11-13 是周五 (第10周)
        tz = get_tz("Asia/Shanghai")
        now_dt = datetime(2026, 11, 13, 8, 30, tzinfo=tz)
        friday_courses = sched.get_courses_for_date(date(2026, 11, 13), now=now_dt)
        self.assertEqual(len(friday_courses), 2)
        self.assertEqual(friday_courses[0].course_name, "数据结构与算法")
        self.assertEqual(friday_courses[0].status, "ongoing")
        self.assertIn("进行中", friday_courses[0].status_text)
        self.assertEqual(friday_courses[1].course_name, "操作系统原理")
        self.assertEqual(friday_courses[1].status, "next")
        self.assertIn("下一节", friday_courses[1].status_text)

    def test_ics_parsing_and_utc_timezone_conversion(self) -> None:
        sched = parse_ics_content(SAMPLE_ICS, tz_name="Asia/Shanghai")
        self.assertEqual(sched.table_name, "2026秋季学期课表")

        # 检查 2026-11-13 (周五) 的 UTC 时间 02:00Z 是否被正确转换为北京时间 10:00
        tz = get_tz("Asia/Shanghai")
        now_dt = datetime(2026, 11, 13, 9, 50, tzinfo=tz)
        courses = sched.get_courses_for_date(date(2026, 11, 13), now=now_dt)
        self.assertEqual(len(courses), 2)
        self.assertEqual(courses[0].course_name, "大学物理实验")
        self.assertEqual(courses[0].start_time, "10:00")
        self.assertEqual(courses[0].end_time, "11:35")
        self.assertEqual(courses[0].teacher, "李老师")
        self.assertEqual(courses[0].status, "next")

    def test_qq_official_identity_and_avatar_urls(self) -> None:
        ev = FakeQQOfficialEvent(
            message_str="/查看课表",
            member_openid="9F8E7D6C5B4A32109F8E7D6C5B4A3210",
            group_openid="GROUP_OPENID_888888",
            sender_name="",  # 模拟 QQ 官方群聊未返回昵称
        )
        ident = extract_event_identity(ev)
        self.assertEqual(ident["platform"], "qq_official")
        self.assertEqual(ident["user_id"], "9F8E7D6C5B4A32109F8E7D6C5B4A3210")
        self.assertEqual(ident["group_id"], "GROUP_OPENID_888888")

        appid = extract_qq_official_appid(event=ev, context=self.ctx)
        self.assertEqual(appid, "102030405")

        prof = self.plugin.storage.get_profile(ident["user_id"])
        # 绝不直接暴露 32 位 OpenID
        self.assertNotEqual(prof.get_display_name(), ident["user_id"])
        self.assertEqual(prof.get_display_name(), "群友#3210")

        urls = build_candidate_avatar_urls(prof, appid=appid)
        self.assertTrue(
            any("thirdqq.qlogo.cn/qqapp/102030405/9F8E7D6C5B4A32109F8E7D6C5B4A3210/100" in u for u in urls)
        )

    async def test_all_commands_and_three_render_styles(self) -> None:
        # 用户 A：通过两步交互发送 .ics 绑定课表，并设置群昵称
        ev1 = FakeQQOfficialEvent(
            message_str="/绑定课表",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
            sender_name="学霸小明",
        )
        res1 = await collect_gen(self.plugin.cmd_bind_schedule(ev1))
        self.assertIn("请在 120 秒内发送", res1[0]["text"])

        ev1_reply = FakeQQOfficialEvent(
            message_str=SAMPLE_ICS,
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
            sender_name="学霸小明",
        )
        res1_reply = await collect_gen(
            self.plugin.on_Fallback_session_message(ev1_reply)
        )
        self.assertIn("课表绑定成功", res1_reply[0]["text"])
        self.assertIn("学霸小明", res1_reply[0]["text"])

        # 用户 B：通过单步内联 WakeUp 数据绑定课表，并通过 /设置昵称 绑定群昵称和 QQ 号
        ev2 = FakeQQOfficialEvent(
            message_str=f"/绑定课表 {SAMPLE_WAKEUP_5LINES}",
            member_openid="OPENID_USER_BBB22222222222222222",
            group_openid="GROUP_OPENID_1001",
            sender_name="",  # 官方机器人未返回昵称
        )
        res2 = await collect_gen(self.plugin.cmd_bind_schedule(ev2))
        self.assertIn("课表绑定成功", res2[0]["text"])

        ev2_nick = FakeQQOfficialEvent(
            message_str="/设置昵称 摸鱼阿华 123456789",
            member_openid="OPENID_USER_BBB22222222222222222",
            group_openid="GROUP_OPENID_1001",
        )
        res_nick = await collect_gen(self.plugin.cmd_set_nickname(ev2_nick))
        self.assertIn("摸鱼阿华", res_nick[0]["text"])
        self.assertIn("123456789", res_nick[0]["text"])

        # 测试图片指令：/查看课表、/今日课表、/明日课表、/本周课表、/下周课表
        for cmd_fn in [
            self.plugin.cmd_view_schedule,
            self.plugin.cmd_today_schedule,
            self.plugin.cmd_tomorrow_schedule,
            self.plugin.cmd_this_week_schedule,
            self.plugin.cmd_next_week_schedule,
        ]:
            out = await collect_gen(cmd_fn(ev1))
            self.assertEqual(out[0]["type"], "image")
            img_file = Path(out[0]["path"])
            self.assertTrue(img_file.exists())
            self.assertGreater(img_file.stat().st_size, 60)

        # 测试群友指令（样式二 & 样式三）：/群友在上什么课、/群友明天上什么课、/本周上课排行
        for group_cmd_fn in [
            self.plugin.cmd_group_current_courses,
            self.plugin.cmd_group_tomorrow_courses,
            self.plugin.cmd_weekly_ranking,
        ]:
            out = await collect_gen(group_cmd_fn(ev2))
            self.assertEqual(out[0]["type"], "image")
            img_file = Path(out[0]["path"])
            self.assertTrue(img_file.exists())
            self.assertGreater(img_file.stat().st_size, 60)

        # 测试 /设置每日推送 （分步回复 "开启 07:00"）
        ev_push = FakeQQOfficialEvent(
            message_str="/设置每日推送",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        await collect_gen(self.plugin.cmd_set_daily_push(ev_push))
        ev_push_reply = FakeQQOfficialEvent(
            message_str="开启 07:00",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_push = await collect_gen(
            self.plugin.on_Fallback_session_message(ev_push_reply)
        )
        self.assertIn("07:00", res_push[0]["text"])

        # 测试 /课前提醒 独立开关 与 /设置提醒时间 （分步回复 "20"）
        ev_rem_toggle = FakeQQOfficialEvent(
            message_str="/课前提醒 开启",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_rem_toggle = await collect_gen(
            self.plugin.cmd_toggle_class_reminder(ev_rem_toggle)
        )
        self.assertIn("已开启课前提醒", res_rem_toggle[0]["text"])

        ev_rem = FakeQQOfficialEvent(
            message_str="/设置提醒时间",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        await collect_gen(self.plugin.cmd_set_reminder_time(ev_rem))
        ev_rem_reply = FakeQQOfficialEvent(
            message_str="20",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_rem = await collect_gen(
            self.plugin.on_Fallback_session_message(ev_rem_reply)
        )
        self.assertIn("提前 20 分钟", res_rem[0]["text"])

        # 测试 /早八提醒、/设置早八提醒时间、/设置早八时间
        ev_e8_thresh = FakeQQOfficialEvent(
            message_str="/设置早八时间 08:15",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_e8_thresh = await collect_gen(
            self.plugin.cmd_set_early_eight_threshold(ev_e8_thresh)
        )
        self.assertIn("08:15", res_e8_thresh[0]["text"])

        ev_e8_time = FakeQQOfficialEvent(
            message_str="/设置早八提醒时间 21:30",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_e8_time = await collect_gen(
            self.plugin.cmd_set_early_eight_remind_time(ev_e8_time)
        )
        self.assertIn("21:30", res_e8_time[0]["text"])

        ev_e8_toggle = FakeQQOfficialEvent(
            message_str="/早八提醒 开启",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_e8_toggle = await collect_gen(
            self.plugin.cmd_toggle_early_eight(ev_e8_toggle)
        )
        self.assertIn("已开启早八提醒", res_e8_toggle[0]["text"])

        # 测试 /周日推送、/设置周日推送时间 与 /下周上课排行
        ev_sun_time = FakeQQOfficialEvent(
            message_str="/设置周日推送时间 20:00",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_sun_time = await collect_gen(
            self.plugin.cmd_set_sunday_push_time(ev_sun_time)
        )
        self.assertIn("20:00", res_sun_time[0]["text"])

        ev_sun_toggle = FakeQQOfficialEvent(
            message_str="/周日推送 开启 20:00",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_sun_toggle = await collect_gen(
            self.plugin.cmd_toggle_sunday_push(ev_sun_toggle)
        )
        self.assertIn("已开启周日下周上课情况推送", res_sun_toggle[0]["text"])

        ev_next_rank = FakeQQOfficialEvent(
            message_str="/下周上课排行",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_next_rank = await collect_gen(
            self.plugin.cmd_next_week_ranking(ev_next_rank)
        )
        self.assertEqual(res_next_rank[0]["type"], "image")
        self.assertTrue(Path(res_next_rank[0]["path"]).exists())

        # 测试 /查看设置
        ev_view_cfg = FakeQQOfficialEvent(
            message_str="/查看设置",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_cfg = await collect_gen(self.plugin.cmd_view_settings(ev_view_cfg))
        self.assertIn("学霸小明", res_cfg[0]["text"])
        self.assertIn("07:00", res_cfg[0]["text"])
        self.assertIn("20 分钟", res_cfg[0]["text"])
        self.assertIn("21:30", res_cfg[0]["text"])
        self.assertIn("08:15", res_cfg[0]["text"])
        self.assertIn("20:00", res_cfg[0]["text"])

        # 测试定时推送、课前@提醒、早八前夜@提醒、周日下周上课情况图片推送
        tz = get_tz("Asia/Shanghai")
        # 1) 早上 07:05 触发每日课表推送
        triggered_morning = await self.plugin.run_scheduled_checks(
            now=datetime(2026, 11, 13, 7, 5, tzinfo=tz)
        )
        self.assertEqual(len(triggered_morning), 1)
        self.assertEqual(triggered_morning[0]["type"], "daily_push")
        self.assertTrue(triggered_morning[0]["sent"])

        # 同一天 07:10 再次检查，不应重复推送
        triggered_dup = await self.plugin.run_scheduled_checks(
            now=datetime(2026, 11, 13, 7, 10, tzinfo=tz)
        )
        self.assertEqual(len(triggered_dup), 0)

        # 2) 09:45（距离 10:00 的《大学物理实验》还有 15 分钟 <= 20 分钟）触发课前提醒，并验证 QQ官方机器人 @某人 语法
        self.ctx.sent_messages.clear()
        triggered_rem = await self.plugin.run_scheduled_checks(
            now=datetime(2026, 11, 13, 9, 45, tzinfo=tz)
        )
        self.assertEqual(len(triggered_rem), 1)
        self.assertEqual(triggered_rem[0]["type"], "class_reminder")
        self.assertEqual(triggered_rem[0]["course_name"], "大学物理实验")
        self.assertIn("15 分钟", triggered_rem[0]["text"])
        sent_plain_rem = self.ctx.sent_messages[-1]["chain"].chain[0].text
        self.assertIn("<@OPENID_USER_AAA11111111111111111>", sent_plain_rem)

        # 3) 2026-11-13 晚上 21:35（前一天晚上提醒第二天 2026-11-14 08:00 的《程序设计实践》早八课，并 @用户）
        self.ctx.sent_messages.clear()
        triggered_e8 = await self.plugin.run_scheduled_checks(
            now=datetime(2026, 11, 13, 21, 35, tzinfo=tz)
        )
        self.assertEqual(len(triggered_e8), 1)
        self.assertEqual(triggered_e8[0]["type"], "early_eight_reminder")
        self.assertEqual(triggered_e8[0]["course_name"], "程序设计实践")
        sent_plain_e8 = self.ctx.sent_messages[-1]["chain"].chain[0].text
        self.assertIn("<@OPENID_USER_AAA11111111111111111>", sent_plain_e8)
        self.assertIn("早八提醒", sent_plain_e8)

        # 4) 2026-11-15（周日）晚上 20:05 触发周日下周上课情况图片自动推送
        self.ctx.sent_messages.clear()
        triggered_sun = await self.plugin.run_scheduled_checks(
            now=datetime(2026, 11, 15, 20, 5, tzinfo=tz)
        )
        self.assertEqual(len(triggered_sun), 1)
        self.assertEqual(triggered_sun[0]["type"], "sunday_next_week_push")
        self.assertTrue(Path(triggered_sun[0]["image_path"]).exists())

        # 5) 测试 /设置当前周 与 /设置总周数 动态调整个人周数后提醒依然有效
        ev_set_wk = FakeQQOfficialEvent(
            message_str="/设置当前周 11 18",
            member_openid="OPENID_USER_AAA11111111111111111",
            group_openid="GROUP_OPENID_1001",
        )
        res_set_wk = await collect_gen(self.plugin.cmd_set_current_week(ev_set_wk))
        self.assertIn("当前第 11 周", res_set_wk[0]["text"])
        self.assertIn("18 周", res_set_wk[0]["text"])

    def test_dynamic_time_slots_durations_breaks_and_weeks(self) -> None:
        """
        验证不同用户的课程开始时间、结束时间、单节时长、每节课间隙、以及不同上课周数的动态自适应：
        1. 河南大学等高校 100分钟(2节, 45m+10m课间) + 150分钟(3节, 10:00-12:30) + 下午 16:40-18:20(9-10节) 的 .ics 课表
        2. 已持久化的旧版 JSON 课表在加载时自动修正 time_slots 与周数
        3. 不同周数与非连续周 (1-8,10-16周) 的精确匹配
        """
        henu_like_ics = "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "X-WR-CALNAME:金明校区动态课表",
                "BEGIN:VEVENT",
                "DTSTART;TZID=Asia/Shanghai:20261110T080000",
                "DTEND;TZID=Asia/Shanghai:20261110T094000",
                "RRULE:FREQ=WEEKLY;COUNT=6",
                "SUMMARY:大学体育（三）",
                "DESCRIPTION:第1 - 2节\\n徐怡康\\n第11-16周",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "DTSTART;TZID=Asia/Shanghai:20261109T100000",
                "DTEND;TZID=Asia/Shanghai:20261109T114000",
                "RRULE:FREQ=WEEKLY;COUNT=6",
                "SUMMARY:数据结构",
                "LOCATION:金明综合楼4101",
                "DESCRIPTION:第3 - 4节\\n金明综合楼4101\\n王龙葛\\n第11-16周",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "DTSTART;TZID=Asia/Shanghai:20261113T100000",
                "DTEND;TZID=Asia/Shanghai:20261113T123000",
                "RRULE:FREQ=WEEKLY;COUNT=6",
                "SUMMARY:毛泽东思想和中国特色社会主义理论体系概论",
                "LOCATION:金明综合楼2101",
                "DESCRIPTION:第3 - 5节\\n金明综合楼2101\\n郑炳凯\\n第11-16周",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "DTSTART;TZID=Asia/Shanghai:20261109T164000",
                "DTEND;TZID=Asia/Shanghai:20261109T182000",
                "RRULE:FREQ=WEEKLY;COUNT=6",
                "SUMMARY:食品中的科技与狠活",
                "LOCATION:金明综合楼3306",
                "DESCRIPTION:第9 - 10节\\n金明综合楼3306\\n马凡怡\\n第11-16周",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "DTSTART;TZID=Asia/Shanghai:20261112T192000",
                "DTEND;TZID=Asia/Shanghai:20261112T210000",
                "RRULE:FREQ=WEEKLY;COUNT=6",
                "SUMMARY:数字逻辑",
                "LOCATION:金明综合楼6503",
                "DESCRIPTION:第11 - 12节\\n金明综合楼6503\\n侯巍\\n第11-16周",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        )

        sched = parse_ics_content(henu_like_ics, tz_name="Asia/Shanghai")
        slot_map = {s.node: s for s in sched.time_slots}

        # 验证根据课表文件动态推算的节次起止时间与课间间隙（不再是写死的 09:50-12:15 或 18:30-19:15）
        self.assertEqual(slot_map[1].start_time, "08:00")
        self.assertEqual(slot_map[2].end_time, "09:40")
        self.assertEqual(slot_map[3].start_time, "10:00")
        self.assertEqual(slot_map[4].end_time, "11:40")
        self.assertEqual(slot_map[5].end_time, "12:30")
        self.assertEqual(slot_map[9].start_time, "16:40")
        self.assertEqual(slot_map[10].end_time, "18:20")
        self.assertEqual(slot_map[11].start_time, "19:20")
        self.assertEqual(slot_map[12].end_time, "21:00")

        # 验证根据 DESCRIPTION 中的“第11-16周”动态锚定当前周数（2026-11-13 为第 11 周，总周数 16 周）
        self.assertEqual(sched.get_week_number(date(2026, 11, 13)), 11)
        self.assertEqual(sched.max_week, 16)

        # 验证周五课程时长为 150 分钟 (2.5小时)
        fri_courses = sched.get_courses_for_date(date(2026, 11, 13))
        self.assertEqual(len(fri_courses), 1)
        self.assertEqual(fri_courses[0].start_time, "10:00")
        self.assertEqual(fri_courses[0].end_time, "12:30")
        self.assertEqual(fri_courses[0].duration_minutes, 150)

        # 验证旧版已存盘的 JSON（带默认节次表）在 ScheduleData.from_dict 加载时自动动态校准
        from core.models import DEFAULT_TIME_SLOTS, ScheduleData

        raw_saved = sched.to_dict()
        raw_saved["time_slots"] = DEFAULT_TIME_SLOTS
        reloaded = ScheduleData.from_dict(raw_saved)
        reloaded_map = {s.node: s for s in reloaded.time_slots}
        self.assertEqual(reloaded_map[3].start_time, "10:00")
        self.assertEqual(reloaded_map[5].end_time, "12:30")
        self.assertEqual(reloaded_map[10].end_time, "18:20")

    def test_pil_rendering_code_paths_with_mock_pil(self) -> None:
        """验证当 Pillow (PIL) 启用时，三种渲染样式的所有绘图分支均无语法或逻辑错误"""
        import core.renderer as rmod
        from core.renderer import create_minimal_png

        class FakeDraw:
            def rectangle(self, *args, **kwargs):
                pass

            def rounded_rectangle(self, *args, **kwargs):
                pass

            def ellipse(self, *args, **kwargs):
                pass

            def line(self, *args, **kwargs):
                pass

            def text(self, *args, **kwargs):
                pass

            def textbbox(self, xy, text, font=None):
                return (0, 0, len(str(text)) * 12, 18)

        class FakeImage:
            class Resampling:
                LANCZOS = 1

            def __init__(self, mode="RGB", size=(800, 600), color=(255, 255, 255)):
                self.mode = mode
                self.size = size

            @classmethod
            def new(cls, mode, size, color=0):
                return cls(mode, size, color)

            @classmethod
            def open(cls, fp):
                return cls("RGBA", (100, 100))

            def convert(self, mode):
                return self

            def resize(self, size, resample=None):
                return FakeImage(self.mode, size)

            def paste(self, im, box=None, mask=None):
                pass

            def save(self, fp, format="PNG"):
                Path(fp).write_bytes(create_minimal_png(self.size[0], self.size[1]))

        class FakeFontObj:
            def getmask(self, text):
                return text.encode("utf-8")

        class FakeFont:
            @staticmethod
            def truetype(path, size, index=0):
                return FakeFontObj()

            @staticmethod
            def load_default():
                return FakeFontObj()

        orig_has_pil = rmod.HAS_PIL
        try:
            rmod.HAS_PIL = True
            rmod.Image = FakeImage
            rmod.ImageDraw = type("ID", (), {"Draw": lambda im: FakeDraw()})
            rmod.ImageFont = FakeFont

            sched_ics = parse_ics_content(SAMPLE_ICS, tz_name="Asia/Shanghai")
            sched_wu = parse_wakeup_raw_data(SAMPLE_WAKEUP_5LINES)
            prof_a = self.plugin.storage.update_user_context(
                "USER_A", group_id="G1", nickname="张同学"
            )
            prof_b = self.plugin.storage.update_user_context(
                "USER_B", group_id="G1", nickname="李同学"
            )
            tz = get_tz("Asia/Shanghai")
            now_dt = datetime(2026, 11, 13, 8, 30, tzinfo=tz)

            p1 = self.plugin.renderer.render_personal_daily(
                prof_a, sched_wu, date(2026, 11, 13), now=now_dt, group_id="G1"
            )
            p2 = self.plugin.renderer.render_personal_weekly(
                prof_a, sched_wu, date(2026, 11, 9), now=now_dt, group_id="G1"
            )
            p3 = self.plugin.renderer.render_group_overview(
                [(prof_a, sched_wu, None), (prof_b, sched_ics, None)],
                group_id="G1",
                now=now_dt,
                is_tomorrow=False,
            )
            p4 = self.plugin.renderer.render_group_overview(
                [(prof_a, sched_wu, None), (prof_b, sched_ics, None)],
                group_id="G1",
                now=now_dt,
                is_tomorrow=True,
            )
            p5 = self.plugin.renderer.render_weekly_ranking(
                [(prof_a, sched_wu, None), (prof_b, sched_ics, None)],
                group_id="G1",
                now=now_dt,
            )
            for p in (p1, p2, p3, p4, p5):
                self.assertTrue(p.exists())
                self.assertGreater(p.stat().st_size, 60)

            # 验证超长课程名与教室名换行完整无截断，且周六/周日课程在周课表中正常获取
            long_course_wu = parse_wakeup_raw_data(
                "\n".join(
                    [
                        json.dumps({"courseLen": 45}),
                        json.dumps(
                            [
                                {"node": 1, "startTime": "08:00", "endTime": "08:45"},
                                {"node": 2, "startTime": "08:50", "endTime": "09:35"},
                                {"node": 3, "startTime": "09:50", "endTime": "10:35"},
                                {"node": 4, "startTime": "10:40", "endTime": "11:25"},
                                {"node": 5, "startTime": "11:30", "endTime": "12:15"},
                            ]
                        ),
                        json.dumps({"startDate": "2026-09-07", "maxWeek": 20, "nodes": 12}),
                        json.dumps(
                            [
                                {"id": 0, "courseName": "毛泽东思想和中国特色社会主义理论体系概论"},
                                {"id": 1, "courseName": "周末创新创业实践与人工智能综合实训课程"},
                            ]
                        ),
                        json.dumps(
                            [
                                {
                                    "id": 0,
                                    "day": 5,
                                    "startNode": 3,
                                    "step": 3,
                                    "startWeek": 1,
                                    "endWeek": 18,
                                    "type": 0,
                                    "room": "金明综合楼2101 郑炳凯",
                                    "teacher": "郑炳凯",
                                },
                                {
                                    "id": 1,
                                    "day": 7,
                                    "startNode": 1,
                                    "step": 2,
                                    "startWeek": 1,
                                    "endWeek": 18,
                                    "type": 0,
                                    "room": "软件学院实训中心502",
                                    "teacher": "林教授",
                                },
                            ]
                        ),
                    ]
                )
            )
            wk_map = long_course_wu.get_courses_for_week(date(2026, 11, 9), now=now_dt)
            self.assertEqual(len(wk_map[5]), 1)
            self.assertEqual(len(wk_map[7]), 1)
            self.assertEqual(
                wk_map[7][0].course_name, "周末创新创业实践与人工智能综合实训课程"
            )

            dummy_draw = FakeDraw()
            card_layout = self.plugin.renderer._measure_course_card(
                dummy_draw, wk_map[5][0], 664
            )
            joined_title = "".join(card_layout["course_lines"])
            self.assertEqual(joined_title, "毛泽东思想和中国特色社会主义理论体系概论")
            joined_meta = "".join(card_layout["meta_lines"])
            self.assertIn("金明综合楼2101 郑炳凯", joined_meta)
        finally:
            rmod.HAS_PIL = orig_has_pil


if __name__ == "__main__":
    unittest.main()
