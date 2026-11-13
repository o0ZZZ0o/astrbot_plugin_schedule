from __future__ import annotations

import asyncio
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import astrbot.api.message_components as Comp  # type: ignore
    from astrbot.api import AstrBotConfig, logger  # type: ignore
    from astrbot.api.event import AstrMessageEvent, MessageChain, filter  # type: ignore
    from astrbot.api.star import Context, Star, StarTools, register  # type: ignore

    try:
        from astrbot.core.utils.session_waiter import (  # type: ignore
            SessionController,
            session_waiter,
        )
    except ImportError:
        from astrbot.api.util import SessionController, session_waiter  # type: ignore

    HAS_ASTRBOT = True
except ImportError:
    HAS_ASTRBOT = False
    import logging

    logger = logging.getLogger("astrbot_plugin_schedule")
    AstrBotConfig = dict  # type: ignore
    AstrMessageEvent = Any  # type: ignore
    SessionController = Any  # type: ignore

    class Context:  # type: ignore
        pass

    class Star:  # type: ignore
        def __init__(self, context: Any = None, *args: Any, **kwargs: Any):
            self.context = context

    class StarTools:  # type: ignore
        @staticmethod
        def get_data_dir(plugin_name: str = "") -> Path:
            return Path(__file__).parent / "data"

    def register(*args: Any, **kwargs: Any):  # type: ignore
        def _decorator(cls: Any) -> Any:
            return cls

        return _decorator

    class _DummyFilter:
        class EventMessageType:
            ALL = "ALL"
            GROUP_MESSAGE = "GROUP_MESSAGE"
            PRIVATE_MESSAGE = "PRIVATE_MESSAGE"

        def command(self, name: str, alias: Optional[set] = None, **kwargs: Any):
            def _dec(fn: Any) -> Any:
                fn._command_name = name
                fn._command_alias = alias or set()
                return fn

            return _dec

        def event_message_type(self, *args: Any, **kwargs: Any):
            def _dec(fn: Any) -> Any:
                return fn

            return _dec

    filter = _DummyFilter()  # type: ignore

    class Comp:  # type: ignore
        class Plain:
            def __init__(self, text: str = ""):
                self.text = text

        class Image:
            def __init__(self, file: str = ""):
                self.file = file

            @classmethod
            def fromFileSystem(cls, path: str) -> "Comp.Image":
                return cls(file=str(path))

        class At:
            def __init__(self, qq: str = "", name: str = ""):
                self.qq = qq
                self.name = name

    class MessageChain:  # type: ignore
        def __init__(self, chain: Optional[List[Any]] = None):
            self.chain: List[Any] = list(chain) if chain else []

        def message(self, text: str) -> "MessageChain":
            self.chain.append(Comp.Plain(text=text))
            return self

        def file_image(self, path: str) -> "MessageChain":
            self.chain.append(Comp.Image.fromFileSystem(str(path)))
            return self

        def use_markdown(self, flag: bool) -> "MessageChain":
            return self

    def session_waiter(timeout: int = 60, record_history_chains: bool = False):  # type: ignore
        def _dec(fn: Any) -> Any:
            return fn

        return _dec

try:
    from .core.models import (
        WEEKDAY_FULL_NAMES,
        CourseInstance,
        ScheduleData,
        UserProfile,
        format_duration_cn,
        now_in_tz,
        parse_hhmm,
    )
    from .core.parser import (
        extract_wakeup_key,
        fetch_and_parse_wakeup,
        parse_ics_content,
    )
    from .core.qq_official import (
        extract_event_identity,
        extract_qq_official_appid,
        extract_schedule_input_from_event,
        fetch_qq_nickname_by_number,
        get_or_download_avatar,
        try_fetch_onebot_group_nickname,
    )
    from .core.renderer import ScheduleRenderer
    from .core.storage import StorageManager
except ImportError:
    from core.models import (  # type: ignore
        WEEKDAY_FULL_NAMES,
        CourseInstance,
        ScheduleData,
        UserProfile,
        format_duration_cn,
        now_in_tz,
        parse_hhmm,
    )
    from core.parser import (  # type: ignore
        extract_wakeup_key,
        fetch_and_parse_wakeup,
        parse_ics_content,
    )
    from core.qq_official import (  # type: ignore
        extract_event_identity,
        extract_qq_official_appid,
        extract_schedule_input_from_event,
        fetch_qq_nickname_by_number,
        get_or_download_avatar,
        try_fetch_onebot_group_nickname,
    )
    from core.renderer import ScheduleRenderer  # type: ignore
    from core.storage import StorageManager  # type: ignore


def parse_valid_hhmm(raw: str) -> Optional[str]:
    """解析并校验 HH:MM 格式时间字符串，成功返回标准化的 HH:MM，失败返回 None"""
    m = re.match(r"^(\d{1,2})\s*[:：点时]\s*(\d{1,2})\s*分?$", raw.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return None


@register(
    "astrbot_plugin_schedule",
    "AstrBot Developer",
    "为AstrBot设计的课程表插件，支持.ics与WakeUp口令绑定、三种精美渲染样式、课前@提醒、早八前夜@提醒、周日下周上课情况排行推送，深度适配QQ官方机器人",
    "1.2.0",
    "https://github.com/astrbot/astrbot_plugin_schedule",
)
class SchedulePlugin(Star):
    """AstrBot 课程表插件主类（适配 QQ 官方机器人与 OneBot）"""

    def __init__(
        self,
        context: Context,
        config: Optional[AstrBotConfig] = None,
        data_dir: Optional[Path] = None,
    ):
        super().__init__(context)
        self.context = context
        self.config: Dict[str, Any] = config if isinstance(config, dict) else {}

        self.plugin_dir = Path(__file__).resolve().parent
        if data_dir is not None:
            resolved_data_dir = Path(data_dir)
        else:
            try:
                resolved_data_dir = Path(StarTools.get_data_dir("astrbot_plugin_schedule"))
            except Exception:
                resolved_data_dir = self.plugin_dir / "data"

        self.storage = StorageManager(resolved_data_dir)
        self.renderer = ScheduleRenderer(self.storage, plugin_dir=self.plugin_dir)

        # 待处理交互会话备份字典: key = (group_id, user_id)
        self._pending_sessions: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # 启动后台定时任务（每日推送、课前@提醒、早八前夜@提醒、周日下周上课情况推送）
        self._scheduler_task: Optional[asyncio.Task] = None
        self._start_scheduler()

    @property
    def timezone(self) -> str:
        return str(self.config.get("timezone") or "Asia/Shanghai").strip()

    @property
    def wakeup_api_url(self) -> str:
        return str(
            self.config.get("wakeup_api_url")
            or "https://i.wakeup.fun/share_schedule/get"
        ).strip()

    @property
    def session_timeout(self) -> int:
        try:
            return max(30, int(self.config.get("session_timeout", 120)))
        except Exception:
            return 120

    def _get_appid(self, event: Optional[AstrMessageEvent] = None) -> str:
        cfg_appid = str(self.config.get("qq_appid") or "").strip()
        return extract_qq_official_appid(
            event=event, context=self.context, config_appid=cfg_appid
        )

    def _start_scheduler(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            self._scheduler_task = loop.create_task(self._scheduler_loop())
        except RuntimeError:
            self._scheduler_task = None

    async def terminate(self) -> None:
        """插件卸载或重启时清理后台定时任务"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

    # =========================================================================
    # 身份同步与通用交互会话辅助方法
    # =========================================================================
    async def _sync_user_from_event(
        self, event: AstrMessageEvent
    ) -> Tuple[UserProfile, Dict[str, str]]:
        ident = extract_event_identity(event)
        user_id = ident["user_id"]
        group_id = ident["group_id"]
        nickname = ident["nickname"]
        avatar_url = ident["avatar_url"]
        umo = ident["umo"]
        qq_number = ident["qq_number"]

        if group_id and (not nickname or nickname == user_id or nickname.isdigit()):
            ob_nick = await try_fetch_onebot_group_nickname(event, group_id, user_id)
            if ob_nick:
                nickname = ob_nick

        prof = self.storage.update_user_context(
            user_id=user_id,
            group_id=group_id or None,
            nickname=nickname or None,
            umo=umo or None,
            avatar_url=avatar_url or None,
            qq_number=qq_number or None,
        )

        if (not prof.nickname or prof._is_raw_id(prof.nickname)) and prof.qq_number:
            fetched_nick = await fetch_qq_nickname_by_number(prof.qq_number)
            if fetched_nick:
                prof = self.storage.set_custom_nickname(
                    user_id=user_id,
                    nickname=fetched_nick,
                    group_id=group_id or None,
                    qq_number=prof.qq_number,
                )

        return prof, ident

    def _extract_command_arg(self, event: AstrMessageEvent, cmd_names: List[str]) -> str:
        """从 event.message_str 中剥离指令前缀与指令名，提取内联参数"""
        raw = str(getattr(event, "message_str", "") or "").strip()
        if not raw:
            return ""
        cleaned = re.sub(r"^@\S+\s*", "", raw).strip()
        for cname in sorted(cmd_names, key=len, reverse=True):
            pat = rf"^[/!！#]?{re.escape(cname)}(?:[\s：:]+|$)(.*)$"
            m = re.match(pat, cleaned, flags=re.DOTALL | re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return ""

    async def _wait_for_user_reply(
        self,
        event: AstrMessageEvent,
        ident: Dict[str, str],
        action_name: str,
        cmd_names: List[str],
        apply_fn: Any,
    ):
        """通用会话等待封装（同时支持 AstrBot session_waiter 与测试环境备用字典）"""
        user_id = ident["user_id"]
        group_id = ident["group_id"]
        umo = ident["umo"]

        if not HAS_ASTRBOT:
            self._pending_sessions[(group_id, user_id)] = {
                "action": action_name,
                "expire": now_in_tz(self.timezone).timestamp() + self.session_timeout,
                "umo": umo,
                "group_id": group_id,
            }
            return

        target_uid = user_id

        @session_waiter(timeout=self.session_timeout, record_history_chains=False)
        async def _waiter(controller: SessionController, wait_event: AstrMessageEvent):
            w_ident = extract_event_identity(wait_event)
            if w_ident["user_id"] != target_uid:
                controller.keep(timeout=self.session_timeout, reset_timeout=False)
                return

            text = str(getattr(wait_event, "message_str", "") or "").strip()
            text = re.sub(r"^@\S+\s*", "", text).strip()
            if text in ("取消", "/取消", "退出", "/退出"):
                await wait_event.send(wait_event.plain_result("✅ 已取消当前设置。"))
                controller.stop()
                return

            w_arg = self._extract_command_arg(wait_event, cmd_names) or text
            w_prof, _ = await self._sync_user_from_event(wait_event)
            ok, reply = apply_fn(
                w_prof,
                w_arg,
                w_ident["umo"] or umo,
                w_ident["group_id"] or group_id,
            )
            await wait_event.send(wait_event.plain_result(reply))
            if ok:
                controller.stop()
            else:
                controller.keep(timeout=self.session_timeout, reset_timeout=True)

        try:
            await _waiter(event)
        except TimeoutError:
            await event.send(event.plain_result("⏰ 设置等待已超时，请重新发送指令。"))
        finally:
            try:
                event.stop_event()
            except Exception:
                pass

    # =========================================================================
    # 1. /绑定课表
    # =========================================================================
    @filter.command("绑定课表")
    async def cmd_bind_schedule(self, event: AstrMessageEvent):
        """在群聊或私聊中发送此命令，通过 .ics 文件或 WakeUp 分享口令绑定个人课表"""
        prof, ident = await self._sync_user_from_event(event)
        user_id = ident["user_id"]
        group_id = ident["group_id"]

        inline_arg = self._extract_command_arg(event, ["绑定课表"])
        ics_content, _ = await extract_schedule_input_from_event(event)

        if ics_content or (
            inline_arg
            and (
                extract_wakeup_key(inline_arg)
                or "BEGIN:VCALENDAR" in inline_arg.upper()
                or ".ics" in inline_arg.lower()
                or inline_arg.startswith("{")
            )
        ):
            ok, reply_msg = await self._process_bind_input(
                event=event,
                prof=prof,
                group_id=group_id,
                ics_content=ics_content,
                text_input=inline_arg,
            )
            yield event.plain_result(reply_msg)
            return

        prompt_lines = [
            "📥 请在 120 秒内发送你的【.ics 日历文件】或粘贴【WakeUp课程表 分享口令】来绑定课表（回复“取消”可退出）。",
            "",
            "💡 绑定方式说明：",
            "1️⃣ WakeUp口令：在 WakeUp 课程表 App 右上角点击「分享」->「复制分享口令」，直接粘贴发送即可（QQ官方机器人群聊中请 @机器人 发送口令，或直接发送：/绑定课表 口令内容）。",
            "2️⃣ .ics文件：直接上传 .ics 日历文件、发送 .ics 下载链接或粘贴 VCALENDAR 内容。",
        ]
        if prof._is_raw_id(prof.nickname):
            prompt_lines.append(
                "3️⃣ 昵称与头像提示：可在消息末尾附带「昵称:你的群昵称 QQ:你的QQ号」，或发送「/设置昵称 群昵称 QQ号」以展示真实群昵称与高清QQ头像！"
            )

        yield event.plain_result("\n".join(prompt_lines))

        if not HAS_ASTRBOT:
            self._pending_sessions[(group_id, user_id)] = {
                "action": "bind_schedule",
                "expire": now_in_tz(self.timezone).timestamp() + self.session_timeout,
                "umo": ident["umo"],
                "group_id": group_id,
            }
            return

        target_uid = user_id

        @session_waiter(timeout=self.session_timeout, record_history_chains=False)
        async def bind_waiter(controller: SessionController, wait_event: AstrMessageEvent):
            w_ident = extract_event_identity(wait_event)
            if w_ident["user_id"] != target_uid:
                controller.keep(timeout=self.session_timeout, reset_timeout=False)
                return

            text = str(getattr(wait_event, "message_str", "") or "").strip()
            if text in ("取消", "/取消", "退出", "/退出"):
                await wait_event.send(wait_event.plain_result("✅ 已取消课表绑定操作。"))
                controller.stop()
                return

            w_ics, w_text = await extract_schedule_input_from_event(wait_event)
            w_arg = self._extract_command_arg(wait_event, ["绑定课表"])
            if w_arg:
                w_text = w_arg

            w_prof, _ = await self._sync_user_from_event(wait_event)
            ok, msg = await self._process_bind_input(
                event=wait_event,
                prof=w_prof,
                group_id=w_ident["group_id"] or group_id,
                ics_content=w_ics,
                text_input=w_text or "",
            )
            await wait_event.send(wait_event.plain_result(msg))
            if ok:
                controller.stop()
            else:
                controller.keep(timeout=self.session_timeout, reset_timeout=True)

        try:
            await bind_waiter(event)
        except TimeoutError:
            yield event.plain_result("⏰ 绑定课表等待已超时，请重新发送 /绑定课表。")
        finally:
            try:
                event.stop_event()
            except Exception:
                pass

    async def _process_bind_input(
        self,
        event: AstrMessageEvent,
        prof: UserProfile,
        group_id: str,
        ics_content: Optional[str],
        text_input: str,
    ) -> Tuple[bool, str]:
        m_nick = re.search(r"(?:昵称|群昵称)[：:\s=]+([^\s,，;；]+)", text_input)
        m_qq = re.search(r"(?:QQ|qq|QQ号)[：:\s=]*(\d{5,12})", text_input)
        if m_nick or m_qq:
            custom_nick = m_nick.group(1).strip() if m_nick else prof.nickname
            custom_qq = m_qq.group(1).strip() if m_qq else prof.qq_number
            prof = self.storage.set_custom_nickname(
                prof.user_id, custom_nick, group_id=group_id or None, qq_number=custom_qq
            )

        try:
            if ics_content:
                schedule = parse_ics_content(ics_content, tz_name=self.timezone)
                source_desc = ".ics 日历文件"
            elif "BEGIN:VCALENDAR" in text_input.upper():
                schedule = parse_ics_content(text_input, tz_name=self.timezone)
                source_desc = ".ics 日历文本"
            elif extract_wakeup_key(text_input) or text_input.strip().startswith("{"):
                schedule = await fetch_and_parse_wakeup(
                    text_input, api_url=self.wakeup_api_url, tz_name=self.timezone
                )
                source_desc = "WakeUp 课程表分享口令"
            else:
                return (
                    False,
                    "⚠️ 未能识别你的输入！请发送有效的 .ics 文件、.ics 下载链接，或包含 32 位密钥的 WakeUp 课程表分享口令（回复“取消”可退出）。",
                )
        except Exception as e:
            return False, f"❌ 课表解析失败：{e}\n请检查文件或口令是否正确（WakeUp 口令有效期为 30 分钟），或回复“取消”退出。"

        self.storage.save_schedule(prof.user_id, schedule)
        appid = self._get_appid(event)
        await get_or_download_avatar(self.storage, prof, appid=appid)

        now = now_in_tz(self.timezone)
        cur_week = schedule.get_week_number(now.date())
        display_name = prof.get_display_name(group_id)
        unique_names = len({c.course_name for c in schedule.courses})

        lines = [
            f"🎉 课表绑定成功！（来源：{source_desc}）",
            f"👤 绑定用户：{display_name}",
            f"📚 课表名称：{schedule.table_name}",
            f"📅 学期基准：{schedule.start_date}（当前为第 {cur_week} 周）",
            f"📊 课程统计：共 {unique_names} 门课程 / {len(schedule.courses)} 条排课记录",
            "",
            "👉 常用指令：/查看课表、/今日课表、/本周课表、/群友在上什么课、/本周上课排行、/课前提醒、/早八提醒、/周日推送",
        ]
        if prof._is_raw_id(prof.nickname):
            lines.append(
                "💡 提示：检测到当前平台未返回你的群昵称，可发送「/设置昵称 你的群昵称 你的QQ号」以在课表和排行榜上展示你的群昵称与QQ头像！"
            )
        return True, "\n".join(lines)

    # =========================================================================
    # 2. /查看课表、/今日课表、/明日课表、/本周课表、/下周课表
    # =========================================================================
    @filter.command("查看课表")
    async def cmd_view_schedule(self, event: AstrMessageEvent):
        """显示自己今天接下来要上的课程（若今日已无课则自动附带明日预告）"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        if not sched:
            yield event.plain_result(
                "⚠️ 你还没有绑定课表哦！请先发送 /绑定课表 上传 .ics 文件或发送 WakeUp 分享口令。"
            )
            return

        now = now_in_tz(self.timezone)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        tomorrow_courses = sched.get_courses_for_date(tomorrow, now=now, tz_name=self.timezone)

        appid = self._get_appid(event)
        avatar_path = await get_or_download_avatar(self.storage, prof, appid=appid)

        img_path = self.renderer.render_personal_daily(
            profile=prof,
            schedule=sched,
            target_date=today,
            now=now,
            group_id=group_id,
            avatar_path=avatar_path,
            title_prefix="今日剩余课程",
            only_upcoming=True,
            tomorrow_preview=tomorrow_courses,
            tz_name=self.timezone,
        )
        yield event.image_result(str(img_path))

    @filter.command("今日课表")
    async def cmd_today_schedule(self, event: AstrMessageEvent):
        """输出今日课表图片"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        if not sched:
            yield event.plain_result("⚠️ 你还没有绑定课表哦！请先发送 /绑定课表 进行绑定。")
            return

        now = now_in_tz(self.timezone)
        appid = self._get_appid(event)
        avatar_path = await get_or_download_avatar(self.storage, prof, appid=appid)

        img_path = self.renderer.render_personal_daily(
            profile=prof,
            schedule=sched,
            target_date=now.date(),
            now=now,
            group_id=group_id,
            avatar_path=avatar_path,
            title_prefix="今日课表",
            only_upcoming=False,
            tz_name=self.timezone,
        )
        yield event.image_result(str(img_path))

    @filter.command("明日课表", alias={"查看明日课表"})
    async def cmd_tomorrow_schedule(self, event: AstrMessageEvent):
        """输出明日课表图片"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        if not sched:
            yield event.plain_result("⚠️ 你还没有绑定课表哦！请先发送 /绑定课表 进行绑定。")
            return

        now = now_in_tz(self.timezone)
        tomorrow = now.date() + timedelta(days=1)
        appid = self._get_appid(event)
        avatar_path = await get_or_download_avatar(self.storage, prof, appid=appid)

        img_path = self.renderer.render_personal_daily(
            profile=prof,
            schedule=sched,
            target_date=tomorrow,
            now=now,
            group_id=group_id,
            avatar_path=avatar_path,
            title_prefix="明日课表",
            only_upcoming=False,
            tz_name=self.timezone,
        )
        yield event.image_result(str(img_path))

    @filter.command("本周课表")
    async def cmd_this_week_schedule(self, event: AstrMessageEvent):
        """输出本周课表图片（完整包含周一至周日）"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        if not sched:
            yield event.plain_result("⚠️ 你还没有绑定课表哦！请先发送 /绑定课表 进行绑定。")
            return

        now = now_in_tz(self.timezone)
        today = now.date()
        monday = today - timedelta(days=today.isoweekday() - 1)
        appid = self._get_appid(event)
        avatar_path = await get_or_download_avatar(self.storage, prof, appid=appid)

        img_path = self.renderer.render_personal_weekly(
            profile=prof,
            schedule=sched,
            monday_date=monday,
            now=now,
            group_id=group_id,
            avatar_path=avatar_path,
            title_prefix="本周课表",
            tz_name=self.timezone,
        )
        yield event.image_result(str(img_path))

    @filter.command("下周课表")
    async def cmd_next_week_schedule(self, event: AstrMessageEvent):
        """输出下周课表图片（完整包含周一至周日）"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        if not sched:
            yield event.plain_result("⚠️ 你还没有绑定课表哦！请先发送 /绑定课表 进行绑定。")
            return

        now = now_in_tz(self.timezone)
        today = now.date()
        next_monday = today - timedelta(days=today.isoweekday() - 1) + timedelta(days=7)
        appid = self._get_appid(event)
        avatar_path = await get_or_download_avatar(self.storage, prof, appid=appid)

        img_path = self.renderer.render_personal_weekly(
            profile=prof,
            schedule=sched,
            monday_date=next_monday,
            now=now,
            group_id=group_id,
            avatar_path=avatar_path,
            title_prefix="下周课表",
            tz_name=self.timezone,
        )
        yield event.image_result(str(img_path))

    # =========================================================================
    # 3. 群友课程与排行榜：/群友在上什么课、/群友明天上什么课、/本周上课排行、/下周上课排行
    # =========================================================================
    @filter.command("群友在上什么课")
    async def cmd_group_current_courses(self, event: AstrMessageEvent):
        """显示群里所有成员当前正在上或下一节要上的课程"""
        _, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        bound_members = self.storage.get_group_bound_members(group_id or None)
        if not bound_members:
            yield event.plain_result(
                "⚠️ 当前群聊还没有成员绑定课表哦！快招呼群友发送 /绑定课表 一起使用吧~"
            )
            return

        appid = self._get_appid(event)
        members_with_avatars: List[Tuple[UserProfile, ScheduleData, Optional[Path]]] = []
        for m_prof, m_sched in bound_members:
            av = await get_or_download_avatar(self.storage, m_prof, appid=appid)
            members_with_avatars.append((m_prof, m_sched, av))

        now = now_in_tz(self.timezone)
        img_path = self.renderer.render_group_overview(
            members_data=members_with_avatars,
            group_id=group_id,
            now=now,
            is_tomorrow=False,
            tz_name=self.timezone,
        )
        yield event.image_result(str(img_path))

    @filter.command("群友明天上什么课")
    async def cmd_group_tomorrow_courses(self, event: AstrMessageEvent):
        """显示群里所有成员明天要上的课程"""
        _, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        bound_members = self.storage.get_group_bound_members(group_id or None)
        if not bound_members:
            yield event.plain_result(
                "⚠️ 当前群聊还没有成员绑定课表哦！快招呼群友发送 /绑定课表 一起使用吧~"
            )
            return

        appid = self._get_appid(event)
        members_with_avatars: List[Tuple[UserProfile, ScheduleData, Optional[Path]]] = []
        for m_prof, m_sched in bound_members:
            av = await get_or_download_avatar(self.storage, m_prof, appid=appid)
            members_with_avatars.append((m_prof, m_sched, av))

        now = now_in_tz(self.timezone)
        img_path = self.renderer.render_group_overview(
            members_data=members_with_avatars,
            group_id=group_id,
            now=now,
            is_tomorrow=True,
            tz_name=self.timezone,
        )
        yield event.image_result(str(img_path))

    @filter.command("本周上课排行")
    async def cmd_weekly_ranking(self, event: AstrMessageEvent):
        """显示本周群友上课时长和节数的排行榜"""
        _, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        bound_members = self.storage.get_group_bound_members(group_id or None)
        if not bound_members:
            yield event.plain_result(
                "⚠️ 当前群聊还没有成员绑定课表哦！请先发送 /绑定课表 绑定后再查看排行榜。"
            )
            return

        appid = self._get_appid(event)
        members_with_avatars: List[Tuple[UserProfile, ScheduleData, Optional[Path]]] = []
        for m_prof, m_sched in bound_members:
            av = await get_or_download_avatar(self.storage, m_prof, appid=appid)
            members_with_avatars.append((m_prof, m_sched, av))

        now = now_in_tz(self.timezone)
        img_path = self.renderer.render_weekly_ranking(
            members_data=members_with_avatars,
            group_id=group_id,
            now=now,
            tz_name=self.timezone,
            title_text="本周群友上课排行榜",
        )
        yield event.image_result(str(img_path))

    @filter.command("下周上课排行", alias={"下周上课情况"})
    async def cmd_next_week_ranking(self, event: AstrMessageEvent):
        """显示下周群友上课时长和节数的排行情况图片（与周日推送样式一致）"""
        _, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        bound_members = self.storage.get_group_bound_members(group_id or None)
        if not bound_members:
            yield event.plain_result(
                "⚠️ 当前群聊还没有成员绑定课表哦！请先发送 /绑定课表 绑定后再查看下周上课情况。"
            )
            return

        appid = self._get_appid(event)
        members_with_avatars: List[Tuple[UserProfile, ScheduleData, Optional[Path]]] = []
        for m_prof, m_sched in bound_members:
            av = await get_or_download_avatar(self.storage, m_prof, appid=appid)
            members_with_avatars.append((m_prof, m_sched, av))

        now = now_in_tz(self.timezone)
        today = now.date()
        next_monday = today - timedelta(days=today.isoweekday() - 1) + timedelta(days=7)
        img_path = self.renderer.render_weekly_ranking(
            members_data=members_with_avatars,
            group_id=group_id,
            now=now,
            tz_name=self.timezone,
            target_monday=next_monday,
            title_text="下周群友上课情况排行",
        )
        yield event.image_result(str(img_path))

    # =========================================================================
    # 4. 每日课表自动推送：/设置每日推送
    # =========================================================================
    @filter.command("设置每日推送")
    async def cmd_set_daily_push(self, event: AstrMessageEvent):
        """配置每日课表自动推送（回复"开启 HH:MM"如"开启 07:00"，或回复"关闭"禁用）"""
        prof, ident = await self._sync_user_from_event(event)
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(event, ["设置每日推送"])
        if inline_arg:
            ok, msg = self._apply_daily_push_setting(
                prof, inline_arg, umo, ident["group_id"]
            )
            yield event.plain_result(msg)
            return

        cur_status = (
            f"已开启（每日 {prof.daily_push_time} 推送）"
            if prof.daily_push_enabled
            else "已关闭"
        )
        yield event.plain_result(
            f"⏰ 当前每日课表推送状态：{cur_status}\n"
            f"请回复“开启 HH:MM”（如“开启 07:00”）启用每日推送，或回复“关闭”禁用（也可直接发送：/设置每日推送 开启 07:00）。"
        )
        await self._wait_for_user_reply(
            event,
            ident,
            "set_daily_push",
            ["设置每日推送"],
            self._apply_daily_push_setting,
        )

    def _apply_daily_push_setting(
        self, prof: UserProfile, user_reply: str, umo: str, group_id: str = ""
    ) -> Tuple[bool, str]:
        cleaned = user_reply.strip().lstrip("/")
        if cleaned in ("关闭", "禁用", "off", "OFF", "false", "0"):
            prof.daily_push_enabled = False
            self.storage.save_profiles()
            return True, "✅ 已关闭每日课表自动推送。"

        if cleaned in ("开启", "启用", "打开", "on", "ON"):
            prof.daily_push_enabled = True
            if umo:
                prof.daily_push_umo = umo
            self.storage.save_profiles()
            return (
                True,
                f"✅ 已开启每日课表自动推送！每天 {prof.daily_push_time} 将自动为你推送当日课表。",
            )

        cleaned_time = re.sub(r"^(?:开启|启用|打开|on|ON)\s*", "", cleaned).strip()
        hhmm = parse_valid_hhmm(cleaned_time)
        if hhmm:
            prof.daily_push_enabled = True
            prof.daily_push_time = hhmm
            if umo:
                prof.daily_push_umo = umo
            self.storage.save_profiles()
            return (
                True,
                f"✅ 已开启每日课表自动推送！每天 {hhmm} 将自动为你推送当日课表。",
            )

        return (
            False,
            "⚠️ 格式不正确！请回复“开启 HH:MM”（例如“开启 07:00”），或回复“关闭”禁用推送。",
        )

    # =========================================================================
    # 5. 课前推送提醒（含 @某人）：/课前提醒（独立开关指令） 与 /设置提醒时间（独立时长指令）
    # =========================================================================
    @filter.command("课前提醒", alias={"设置课前提醒", "上课提醒"})
    async def cmd_toggle_class_reminder(self, event: AstrMessageEvent):
        """单独控制开课前自动推送提醒的开关（开启后将在开课前自动推送并 @你）"""
        prof, ident = await self._sync_user_from_event(event)
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(
            event, ["课前提醒", "设置课前提醒", "上课提醒"]
        )
        if inline_arg:
            ok, msg = self._apply_reminder_toggle(
                prof, inline_arg, umo, ident["group_id"]
            )
            yield event.plain_result(msg)
            return

        cur_state = (
            f"已开启（开课前 {prof.reminder_minutes} 分钟推送并 @你）"
            if prof.reminder_enabled
            else f"已关闭（预设时长：提前 {prof.reminder_minutes} 分钟）"
        )
        yield event.plain_result(
            f"🔔 当前课前提醒状态：{cur_state}\n"
            f"请回复“开启”或“关闭”来控制课前提醒开关（如需修改提前分钟数请发送 /设置提醒时间）。"
        )
        await self._wait_for_user_reply(
            event,
            ident,
            "toggle_reminder",
            ["课前提醒", "设置课前提醒", "上课提醒"],
            self._apply_reminder_toggle,
        )

    def _apply_reminder_toggle(
        self, prof: UserProfile, user_reply: str, umo: str, group_id: str = ""
    ) -> Tuple[bool, str]:
        cleaned = user_reply.strip().lstrip("/")
        if cleaned in ("关闭", "禁用", "off", "OFF", "false", "0"):
            prof.reminder_enabled = False
            self.storage.save_profiles()
            return True, "✅ 已关闭开课前提醒功能。"

        # 支持 "开启" 或 "开启 20"
        m = re.match(
            r"^(?:开启|启用|打开|on|ON)(?:\s+(\d{1,3})\s*(?:分钟|min|m)?)?$",
            cleaned,
            re.IGNORECASE,
        )
        if m:
            if m.group(1):
                mins = int(m.group(1))
                if not (1 <= mins <= 120):
                    return False, "⚠️ 提前提醒分钟数需在 1 到 120 之间！"
                prof.reminder_minutes = mins
            elif prof.reminder_minutes <= 0:
                prof.reminder_minutes = 15
            prof.reminder_enabled = True
            if umo:
                prof.reminder_umo = umo
            self.storage.save_profiles()
            return (
                True,
                f"✅ 已开启课前提醒！将在每节课开课前 {prof.reminder_minutes} 分钟在群内 @你 发送提醒（可通过 /设置提醒时间 修改提前分钟数）。",
            )

        return False, "⚠️ 格式不正确！请回复“开启”或“关闭”来切换课前提醒开关。"

    @filter.command("设置提醒时间", alias={"设置课前提醒时间"})
    async def cmd_set_reminder_time(self, event: AstrMessageEvent):
        """单独配置开课前提醒的提前分钟数（范围 1-120 分钟）"""
        prof, ident = await self._sync_user_from_event(event)
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(
            event, ["设置提醒时间", "设置课前提醒时间"]
        )
        if inline_arg:
            ok, msg = self._apply_reminder_setting(
                prof, inline_arg, umo, ident["group_id"]
            )
            yield event.plain_result(msg)
            return

        cur_rem = (
            f"已开启（开课前 {prof.reminder_minutes} 分钟提醒）"
            if prof.reminder_enabled
            else f"未开启（当前设定时长为 {prof.reminder_minutes} 分钟）"
        )
        yield event.plain_result(
            f"🔔 当前课前提醒配置：{cur_rem}\n"
            f"请回复开课前提前提醒的分钟数（范围 1-120，如回复“15”表示课前 15 分钟提醒；也可通过 /课前提醒 控制开关）。"
        )
        await self._wait_for_user_reply(
            event,
            ident,
            "set_reminder_time",
            ["设置提醒时间", "设置课前提醒时间"],
            self._apply_reminder_setting,
        )

    def _apply_reminder_setting(
        self, prof: UserProfile, user_reply: str, umo: str, group_id: str = ""
    ) -> Tuple[bool, str]:
        cleaned = user_reply.strip().lstrip("/")
        if cleaned in ("关闭", "禁用", "0", "off", "OFF", "false"):
            prof.reminder_enabled = False
            self.storage.save_profiles()
            return True, "✅ 已关闭开课前提醒功能。"

        m = re.match(r"^(\d{1,3})\s*(?:分钟|min|m)?$", cleaned, re.IGNORECASE)
        if m:
            mins = int(m.group(1))
            if 1 <= mins <= 120:
                prof.reminder_minutes = mins
                prof.reminder_enabled = True
                if umo:
                    prof.reminder_umo = umo
                self.storage.save_profiles()
                return (
                    True,
                    f"✅ 已将开课前提醒时长设置为：提前 {mins} 分钟（课前提醒已同步开启，届时将自动 @你）！",
                )
            return False, "⚠️ 提醒时间超出范围！请输入 1 到 120 之间的整数分钟数。"

        return False, "⚠️ 格式不正确！请回复 1-120 之间的分钟数字（例如“15”）。"

    # =========================================================================
    # 6. 早八提醒（含 @某人）：/早八提醒、/设置早八提醒时间、/设置早八时间
    # =========================================================================
    @filter.command("早八提醒", alias={"设置早八提醒"})
    async def cmd_toggle_early_eight(self, event: AstrMessageEvent):
        """控制早八提醒开关（在有早八课的前一天指定时间推送提醒并 @你）"""
        prof, ident = await self._sync_user_from_event(event)
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(event, ["早八提醒", "设置早八提醒"])
        if inline_arg:
            ok, msg = self._apply_early_eight_toggle(
                prof, inline_arg, umo, ident["group_id"]
            )
            yield event.plain_result(msg)
            return

        cur_state = "已开启" if prof.early_eight_enabled else "已关闭"
        yield event.plain_result(
            f"🌅 当前早八提醒状态：{cur_state}\n"
            f"• 前一天提醒时间：{prof.early_eight_remind_time}（可通过 /设置早八提醒时间 修改）\n"
            f"• 早八课程判定标准：{prof.early_eight_threshold} 及之前开始的课（可通过 /设置早八时间 修改）\n\n"
            f"请回复“开启”（或“开启 21:30”）启用早八提醒，或回复“关闭”禁用。"
        )
        await self._wait_for_user_reply(
            event,
            ident,
            "toggle_early_eight",
            ["早八提醒", "设置早八提醒"],
            self._apply_early_eight_toggle,
        )

    def _apply_early_eight_toggle(
        self, prof: UserProfile, user_reply: str, umo: str, group_id: str = ""
    ) -> Tuple[bool, str]:
        cleaned = user_reply.strip().lstrip("/")
        if cleaned in ("关闭", "禁用", "off", "OFF", "false", "0"):
            prof.early_eight_enabled = False
            self.storage.save_profiles()
            return True, "✅ 已关闭早八前夜提醒功能。"

        if cleaned in ("开启", "启用", "打开", "on", "ON"):
            prof.early_eight_enabled = True
            if umo:
                prof.early_eight_umo = umo
            self.storage.save_profiles()
            return (
                True,
                f"✅ 已开启早八提醒！若次日有 {prof.early_eight_threshold} 及之前开始的早八课，将在前一天 {prof.early_eight_remind_time} 自动 @你 提醒。",
            )

        # 支持 "开启 21:30" 或 "开启 21:30 08:30"
        m = re.match(
            r"^(?:开启|启用|打开|on|ON)?\s*(\d{1,2}[:：点时]\d{1,2}分?)(?:\s+(\d{1,2}[:：点时]\d{1,2}分?))?$",
            cleaned,
        )
        if m:
            t1 = parse_valid_hhmm(m.group(1))
            t2 = parse_valid_hhmm(m.group(2)) if m.group(2) else None
            if not t1 or (m.group(2) and not t2):
                return False, "⚠️ 时间格式无效，请使用 HH:MM 格式（如 21:30）。"
            prof.early_eight_enabled = True
            prof.early_eight_remind_time = t1
            if t2:
                prof.early_eight_threshold = t2
            if umo:
                prof.early_eight_umo = umo
            self.storage.save_profiles()
            return (
                True,
                f"✅ 已开启早八提醒！当前设定：次日 {prof.early_eight_threshold} 及之前开始的课程算早八，将在前一天 {prof.early_eight_remind_time} 自动 @你 提醒。",
            )

        return (
            False,
            "⚠️ 格式不正确！请回复“开启”、“开启 HH:MM”（如“开启 21:30”）或“关闭”。",
        )

    @filter.command("设置早八提醒时间")
    async def cmd_set_early_eight_remind_time(self, event: AstrMessageEvent):
        """设置在有早八那一天的前一天几点进行提醒（如 21:30）"""
        prof, ident = await self._sync_user_from_event(event)
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(event, ["设置早八提醒时间"])
        if inline_arg:
            ok, msg = self._apply_early_eight_remind_time(
                prof, inline_arg, umo, ident["group_id"]
            )
            yield event.plain_result(msg)
            return

        yield event.plain_result(
            f"⏰ 当前早八前一天提醒时间：{prof.early_eight_remind_time}\n"
            f"请回复希望在前一天几点进行提醒（格式 HH:MM，如“21:30”或“22:00”）。"
        )
        await self._wait_for_user_reply(
            event,
            ident,
            "set_early_eight_remind_time",
            ["设置早八提醒时间"],
            self._apply_early_eight_remind_time,
        )

    def _apply_early_eight_remind_time(
        self, prof: UserProfile, user_reply: str, umo: str, group_id: str = ""
    ) -> Tuple[bool, str]:
        cleaned = re.sub(
            r"^(?:开启|启用|设置)?\s*", "", user_reply.strip().lstrip("/")
        ).strip()
        hhmm = parse_valid_hhmm(cleaned)
        if not hhmm:
            return False, "⚠️ 时间格式无效！请输入 HH:MM 格式的时间（例如“21:30”或“22:00”）。"
        prof.early_eight_remind_time = hhmm
        prof.early_eight_enabled = True
        if umo:
            prof.early_eight_umo = umo
        self.storage.save_profiles()
        return (
            True,
            f"✅ 已将早八前一天提醒时间设置为：{hhmm}（早八提醒已同步开启，将在有早八课的前一天 {hhmm} 自动 @你）！",
        )

    @filter.command("设置早八时间", alias={"设置早八判定时间", "设置早八阈值"})
    async def cmd_set_early_eight_threshold(self, event: AstrMessageEvent):
        """设置什么时候开始的课程算作早八（如 08:00 或 08:30）"""
        prof, ident = await self._sync_user_from_event(event)
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(
            event, ["设置早八时间", "设置早八判定时间", "设置早八阈值"]
        )
        if inline_arg:
            ok, msg = self._apply_early_eight_threshold(
                prof, inline_arg, umo, ident["group_id"]
            )
            yield event.plain_result(msg)
            return

        yield event.plain_result(
            f"🌅 当前早八课程判定标准：{prof.early_eight_threshold} 及之前开始的课程算作早八。\n"
            f"请回复新的早八判定时间（格式 HH:MM，如“08:00”或“08:30”）。"
        )
        await self._wait_for_user_reply(
            event,
            ident,
            "set_early_eight_threshold",
            ["设置早八时间", "设置早八判定时间", "设置早八阈值"],
            self._apply_early_eight_threshold,
        )

    def _apply_early_eight_threshold(
        self, prof: UserProfile, user_reply: str, umo: str, group_id: str = ""
    ) -> Tuple[bool, str]:
        cleaned = re.sub(
            r"^(?:设置)?\s*", "", user_reply.strip().lstrip("/")
        ).strip()
        hhmm = parse_valid_hhmm(cleaned)
        if not hhmm:
            return False, "⚠️ 时间格式无效！请输入 HH:MM 格式的时间（例如“08:00”或“08:30”）。"
        prof.early_eight_threshold = hhmm
        if umo and not prof.early_eight_umo:
            prof.early_eight_umo = umo
        self.storage.save_profiles()
        return (
            True,
            f"✅ 已将早八判定标准设置为：{hhmm}（即早上 {hhmm} 及之前开始的课程均算作早八课）！",
        )

    # =========================================================================
    # 7. 周日推送下周上课情况：/周日推送、/设置周日推送时间
    # =========================================================================
    @filter.command("周日推送", alias={"设置周日推送", "下周课表推送"})
    async def cmd_toggle_sunday_push(self, event: AstrMessageEvent):
        """控制周日自动推送下周上课情况排行图的开关与时间设定"""
        prof, ident = await self._sync_user_from_event(event)
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(
            event, ["周日推送", "设置周日推送", "下周课表推送"]
        )
        if inline_arg:
            ok, msg = self._apply_sunday_push_setting(
                prof, inline_arg, umo, ident["group_id"]
            )
            yield event.plain_result(msg)
            return

        cur_state = (
            f"已开启（每周日 {prof.sunday_push_time} 自动推送下周上课情况图片）"
            if prof.sunday_push_enabled
            else f"已关闭（预设时间：每周日 {prof.sunday_push_time}）"
        )
        yield event.plain_result(
            f"📊 当前周日下周上课情况推送状态：{cur_state}\n"
            f"请回复“开启”、“开启 HH:MM”（如“开启 20:00”）启用周日推送，或回复“关闭”禁用。"
        )
        await self._wait_for_user_reply(
            event,
            ident,
            "toggle_sunday_push",
            ["周日推送", "设置周日推送", "下周课表推送"],
            self._apply_sunday_push_setting,
        )

    def _apply_sunday_push_setting(
        self, prof: UserProfile, user_reply: str, umo: str, group_id: str = ""
    ) -> Tuple[bool, str]:
        cleaned = user_reply.strip().lstrip("/")
        if cleaned in ("关闭", "禁用", "off", "OFF", "false", "0"):
            prof.sunday_push_enabled = False
            self.storage.save_profiles()
            return True, "✅ 已关闭周日下周上课情况自动推送。"

        if cleaned in ("开启", "启用", "打开", "on", "ON"):
            prof.sunday_push_enabled = True
            if umo:
                prof.sunday_push_umo = umo
            if group_id:
                prof.sunday_push_group_id = group_id
            self.storage.save_profiles()
            return (
                True,
                f"✅ 已开启周日下周上课情况推送！每周日 {prof.sunday_push_time} 将自动生成并推送《下周群友上课情况排行》图片。",
            )

        cleaned_time = re.sub(r"^(?:开启|启用|打开|on|ON)\s*", "", cleaned).strip()
        hhmm = parse_valid_hhmm(cleaned_time)
        if hhmm:
            prof.sunday_push_enabled = True
            prof.sunday_push_time = hhmm
            if umo:
                prof.sunday_push_umo = umo
            if group_id:
                prof.sunday_push_group_id = group_id
            self.storage.save_profiles()
            return (
                True,
                f"✅ 已开启周日下周上课情况推送！每周日 {hhmm} 将自动生成并推送《下周群友上课情况排行》图片。",
            )

        return (
            False,
            "⚠️ 格式不正确！请回复“开启”、“开启 HH:MM”（如“开启 20:00”）或“关闭”。",
        )

    @filter.command("设置周日推送时间")
    async def cmd_set_sunday_push_time(self, event: AstrMessageEvent):
        """单独设置周日推送下周上课情况的时间点（如 20:00）"""
        prof, ident = await self._sync_user_from_event(event)
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(event, ["设置周日推送时间"])
        if inline_arg:
            ok, msg = self._apply_sunday_push_time_only(
                prof, inline_arg, umo, ident["group_id"]
            )
            yield event.plain_result(msg)
            return

        yield event.plain_result(
            f"📊 当前周日下周上课情况推送时间：{prof.sunday_push_time}\n"
            f"请回复新的周日推送时间（格式 HH:MM，如“20:00”）。"
        )
        await self._wait_for_user_reply(
            event,
            ident,
            "set_sunday_push_time",
            ["设置周日推送时间"],
            self._apply_sunday_push_time_only,
        )

    def _apply_sunday_push_time_only(
        self, prof: UserProfile, user_reply: str, umo: str, group_id: str = ""
    ) -> Tuple[bool, str]:
        cleaned = re.sub(
            r"^(?:开启|启用|设置)?\s*", "", user_reply.strip().lstrip("/")
        ).strip()
        hhmm = parse_valid_hhmm(cleaned)
        if not hhmm:
            return False, "⚠️ 时间格式无效！请输入 HH:MM 格式的时间（例如“20:00”）。"
        prof.sunday_push_time = hhmm
        prof.sunday_push_enabled = True
        if umo:
            prof.sunday_push_umo = umo
        if group_id:
            prof.sunday_push_group_id = group_id
        self.storage.save_profiles()
        return (
            True,
            f"✅ 已将周日下周上课情况推送时间设置为：每周日 {hhmm}（周日推送已同步开启）！",
        )

    # =========================================================================
    # 8. /查看设置、/设置昵称、/解绑课表、/课表帮助
    # =========================================================================
    @filter.command("查看设置")
    async def cmd_view_settings(self, event: AstrMessageEvent):
        """查看当前所有课表与提醒配置"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        now = now_in_tz(self.timezone)

        display_name = prof.get_display_name(group_id)
        if sched:
            cur_week = sched.get_week_number(now.date())
            sched_status = (
                f"已绑定（{sched.table_name} · 来源 {sched.source_type.upper()} · 当前第 {cur_week} 周 · 共 {len(sched.courses)} 条排课）"
            )
        else:
            sched_status = "未绑定（发送 /绑定课表 即可绑定）"

        push_status = (
            f"已开启（每天 {prof.daily_push_time} 自动推送）"
            if prof.daily_push_enabled
            else "未开启（/设置每日推送）"
        )
        reminder_status = (
            f"已开启（开课前 {prof.reminder_minutes} 分钟 @你 提醒）"
            if prof.reminder_enabled
            else f"未开启（预设提前 {prof.reminder_minutes} 分钟 · /课前提醒 开启）"
        )
        early_status = (
            f"已开启（前一天 {prof.early_eight_remind_time} @你 提醒 · {prof.early_eight_threshold} 及之前算早八）"
            if prof.early_eight_enabled
            else f"未开启（前一天 {prof.early_eight_remind_time} · {prof.early_eight_threshold} 算早八 · /早八提醒 开启）"
        )
        sunday_status = (
            f"已开启（每周日 {prof.sunday_push_time} 推送下周上课情况图）"
            if prof.sunday_push_enabled
            else f"未开启（预设周日 {prof.sunday_push_time} · /周日推送 开启）"
        )
        qq_status = prof.qq_number if prof.qq_number else "未关联（可发送 /设置昵称 群昵称 QQ号 关联）"

        lines = [
            "⚙️ 【个人课表与通知设置】",
            f"👤 显示群昵称：{display_name}",
            f"🐧 关联QQ号：{qq_status}",
            f"📚 课表状态：{sched_status}",
            f"⏰ 每日推送：{push_status}",
            f"🔔 课前提醒：{reminder_status}",
            f"🌅 早八提醒：{early_status}",
            f"📊 周日推送：{sunday_status}",
            f"🌍 当前时区：{self.timezone} ({now.strftime('%Y-%m-%d %H:%M')})",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("设置昵称", alias={"修改昵称", "绑定QQ"})
    async def cmd_set_nickname(self, event: AstrMessageEvent):
        """设置群昵称及可选的数字QQ号（用于QQ官方机器人下展示群昵称与高清QQ头像）"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        arg = self._extract_command_arg(event, ["设置昵称", "修改昵称", "绑定QQ"])
        if not arg:
            yield event.plain_result(
                "💡 请在指令后输入你的群昵称（可选附带真实QQ号以获取高清头像）：\n"
                "示例1：/设置昵称 小明\n"
                "示例2：/设置昵称 小明 12345678"
            )
            return

        parts = arg.split()
        new_nick = parts[0].strip()
        new_qq = ""
        if len(parts) >= 2 and parts[1].strip().isdigit():
            new_qq = parts[1].strip()
        elif new_nick.isdigit() and 5 <= len(new_nick) <= 12:
            new_qq = new_nick
            fetched = await fetch_qq_nickname_by_number(new_qq)
            new_nick = fetched or prof.nickname or f"群友({new_qq[-4:]})"

        prof = self.storage.set_custom_nickname(
            user_id=prof.user_id,
            nickname=new_nick,
            group_id=group_id or None,
            qq_number=new_qq or None,
        )
        cache_file = self.storage.avatars_dir / f"{prof.user_id}.img"
        if cache_file.exists():
            try:
                cache_file.unlink()
            except Exception:
                pass
        appid = self._get_appid(event)
        await get_or_download_avatar(self.storage, prof, appid=appid, cache_ttl=0)

        msg = f"✅ 设置成功！当前显示群昵称：{prof.get_display_name(group_id)}"
        if prof.qq_number:
            msg += f"，已关联 QQ 号：{prof.qq_number}（已同步 QQ 头像）"
        yield event.plain_result(msg)

    @filter.command("解绑课表", alias={"删除课表"})
    async def cmd_unbind_schedule(self, event: AstrMessageEvent):
        """解绑并删除个人课表数据"""
        prof, _ = await self._sync_user_from_event(event)
        removed = self.storage.delete_schedule(prof.user_id)
        if removed:
            yield event.plain_result("✅ 已成功解绑并清除你的个人课表数据。")
        else:
            yield event.plain_result("ℹ️ 你当前尚未绑定课表。")

    @filter.command("课表帮助", alias={"课程表", "课程表帮助"})
    async def cmd_schedule_help(self, event: AstrMessageEvent):
        """显示课程表插件所有指令说明"""
        help_lines = [
            "📖 【AstrBot 课程表插件指令列表 (v1.2.0)】",
            "1. /绑定课表 ：发送 .ics 文件或 WakeUp 分享口令绑定课表",
            "2. /查看课表 ：显示自己今天接下来要上的课程（图片）",
            "3. /今日课表 ：输出今日完整课表图片",
            "4. /明日课表 ：输出明日课表图片",
            "5. /本周课表 ：输出本周课表网格图片（含周六周日）",
            "6. /下周课表 ：输出下周课表网格图片（含周六周日）",
            "7. /群友在上什么课 ：查看群内所有已绑定成员当前上课/空闲状态",
            "8. /群友明天上什么课 ：查看群内所有成员明天的课程安排",
            "9. /本周上课排行 ：查看本周群友上课时长与节数排行榜",
            "10. /下周上课排行 ：查看下周群友上课情况排行榜",
            "11. /设置每日推送 ：配置每日课表推送（回复“开启 07:00”或“关闭”）",
            "12. /课前提醒 ：控制课前自动提醒并 @你 的开关（回复“开启”或“关闭”）",
            "13. /设置提醒时间 ：设置开课前多少分钟提醒（回复 1-120）",
            "14. /早八提醒 ：控制早八前夜自动 @你 提醒的开关（回复“开启”或“关闭”）",
            "15. /设置早八提醒时间 ：设置在有早八的前一天几点提醒（如 21:30）",
            "16. /设置早八时间 ：设置几点及之前开始的课算早八（如 08:00）",
            "17. /周日推送 ：控制周日自动推送下周上课情况图的开关与时间",
            "18. /设置周日推送时间 ：设置周日几点推送下周上课情况（如 20:00）",
            "19. /查看设置 ：查看当前所有配置",
            "20. /设置昵称 <群昵称> [QQ号] ：自定义群昵称与关联QQ头像",
            "21. /解绑课表 ：清除已绑定的个人课表",
        ]
        yield event.plain_result("\n".join(help_lines))

    # =========================================================================
    # 备用会话消息捕获（兼容非 session_waiter 环境或独立测试）
    # =========================================================================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_Fallback_session_message(self, event: AstrMessageEvent):
        if not self._pending_sessions:
            return
        ident = extract_event_identity(event)
        key = (ident["group_id"], ident["user_id"])
        session_info = self._pending_sessions.get(key) or self._pending_sessions.get(
            ("", ident["user_id"])
        )
        if not session_info:
            return

        now_ts = now_in_tz(self.timezone).timestamp()
        if now_ts > session_info.get("expire", 0):
            self._pending_sessions.pop(key, None)
            self._pending_sessions.pop(("", ident["user_id"]), None)
            return

        text = str(getattr(event, "message_str", "") or "").strip()
        if text.startswith("/") and not any(
            text.startswith(p) for p in ("/开启", "/关闭", "/取消")
        ):
            return

        action = session_info["action"]
        prof, _ = await self._sync_user_from_event(event)
        umo = ident["umo"] or session_info.get("umo", "")
        gid = ident["group_id"] or session_info.get("group_id", "")

        if text in ("取消", "/取消", "退出", "/退出"):
            self._pending_sessions.pop(key, None)
            self._pending_sessions.pop(("", ident["user_id"]), None)
            yield event.plain_result("✅ 已取消当前操作。")
            return

        action_handlers = {
            "set_daily_push": self._apply_daily_push_setting,
            "toggle_reminder": self._apply_reminder_toggle,
            "set_reminder_time": self._apply_reminder_setting,
            "toggle_early_eight": self._apply_early_eight_toggle,
            "set_early_eight_remind_time": self._apply_early_eight_remind_time,
            "set_early_eight_threshold": self._apply_early_eight_threshold,
            "toggle_sunday_push": self._apply_sunday_push_setting,
            "set_sunday_push_time": self._apply_sunday_push_time_only,
        }

        if action == "bind_schedule":
            ics_c, txt_c = await extract_schedule_input_from_event(event)
            ok, msg = await self._process_bind_input(
                event, prof, gid, ics_c, txt_c or text
            )
            if ok:
                self._pending_sessions.pop(key, None)
                self._pending_sessions.pop(("", ident["user_id"]), None)
            yield event.plain_result(msg)
        elif action in action_handlers:
            ok, msg = action_handlers[action](prof, text, umo, gid)
            if ok:
                self._pending_sessions.pop(key, None)
                self._pending_sessions.pop(("", ident["user_id"]), None)
            yield event.plain_result(msg)

    # =========================================================================
    # 定时任务调度：
    # 1) 每日课表自动推送
    # 2) 开课前推送提醒（支持 QQ 官方机器人 @某人）
    # 3) 早八前一天定时提醒（支持 QQ 官方机器人 @某人）
    # 4) 周日推送下周上课情况排行图
    # =========================================================================
    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.run_scheduled_checks()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[astrbot_plugin_schedule] 定时任务执行异常: {e}")
            await asyncio.sleep(30)

    async def run_scheduled_checks(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """执行一次全量定时任务检查，返回本次触发的所有推送事件列表"""
        if now is None:
            now = now_in_tz(self.timezone)
        today = now.date()
        today_str = today.isoformat()
        tomorrow = today + timedelta(days=1)
        tomorrow_weekday_str = WEEKDAY_FULL_NAMES[tomorrow.isoweekday() - 1]
        cur_hhmm = now.strftime("%H:%M")

        triggered_events: List[Dict[str, Any]] = []
        bound_users = self.storage.get_all_bound_users()
        appid = self._get_appid()

        # 用于记录本次周日已推送过的群聊/会话，避免同一群内多人开启时重复推送
        pushed_sunday_targets: Set[str] = set()

        for prof, sched in bound_users:
            day_courses = sched.get_courses_for_date(
                today, now=now, tz_name=self.timezone
            )
            display_name = prof.get_display_name()

            # -----------------------------------------------------------------
            # 1. 每日课表自动推送
            # -----------------------------------------------------------------
            if (
                prof.daily_push_enabled
                and prof.daily_push_time
                and prof.last_daily_push_date != today_str
            ):
                if self._is_time_reached(cur_hhmm, prof.daily_push_time):
                    prof.last_daily_push_date = today_str
                    self.storage.save_profiles()

                    av_path = await get_or_download_avatar(
                        self.storage, prof, appid=appid
                    )
                    img_path = self.renderer.render_personal_daily(
                        profile=prof,
                        schedule=sched,
                        target_date=today,
                        now=now,
                        avatar_path=av_path,
                        title_prefix="每日课表推送",
                        only_upcoming=False,
                        tz_name=self.timezone,
                    )
                    if day_courses:
                        first_c = day_courses[0]
                        summary_txt = (
                            f"☀️ 早安，{display_name}！今天你有 {len(day_courses)} 门课，第一节《{first_c.course_name}》将于 {first_c.start_time} 在 {first_c.room or '未指定教室'} 开始。"
                        )
                    else:
                        summary_txt = f"☀️ 早安，{display_name}！今天全天没有课程安排，祝你度过愉快的一天~"

                    target_umo = prof.daily_push_umo or prof.user_umo
                    sent = await self._send_proactive_message(
                        target_umo=target_umo,
                        text=summary_txt,
                        image_path=img_path,
                        prof=prof,
                    )
                    triggered_events.append(
                        {
                            "type": "daily_push",
                            "user_id": prof.user_id,
                            "umo": target_umo,
                            "image_path": str(img_path),
                            "text": summary_txt,
                            "sent": sent,
                        }
                    )

            # -----------------------------------------------------------------
            # 2. 开课前推送提醒（支持 QQ 官方机器人 @某人）
            # -----------------------------------------------------------------
            if (
                prof.reminder_enabled
                and 1 <= prof.reminder_minutes <= 120
                and day_courses
            ):
                prof.reminded_classes = [
                    k for k in prof.reminded_classes if k.startswith(today_str)
                ]
                for c in day_courses:
                    if c.start_dt <= now:
                        continue
                    diff_mins = (c.start_dt - now).total_seconds() / 60.0
                    if (
                        0 < diff_mins <= prof.reminder_minutes
                        and c.unique_key not in prof.reminded_classes
                    ):
                        prof.reminded_classes.append(c.unique_key)
                        self.storage.save_profiles()

                        rem_mins_int = max(1, math.ceil(diff_mins))
                        rem_text = (
                            f"🔔 【上课提醒】{display_name}，距离《{c.course_name}》开课还有 {rem_mins_int} 分钟！\n"
                            f"🕒 时间：{c.start_time} - {c.end_time} ({c.node_range_str})\n"
                            f"📍 教室：{c.room or '未指定'}\n"
                            f"👤 教师：{c.teacher or '未注明'}"
                        )
                        target_umo = prof.reminder_umo or prof.user_umo
                        sent = await self._send_proactive_message(
                            target_umo=target_umo,
                            text=rem_text,
                            image_path=None,
                            prof=prof,
                            mention_user_id=prof.user_id,
                        )
                        triggered_events.append(
                            {
                                "type": "class_reminder",
                                "user_id": prof.user_id,
                                "course_name": c.course_name,
                                "umo": target_umo,
                                "text": rem_text,
                                "mention_user_id": prof.user_id,
                                "sent": sent,
                            }
                        )

            # -----------------------------------------------------------------
            # 3. 早八前一天提醒（在有早八课的前一天指定时间推送并 @某人）
            # -----------------------------------------------------------------
            if (
                prof.early_eight_enabled
                and prof.early_eight_remind_time
                and prof.last_early_eight_remind_date != today_str
            ):
                if self._is_time_reached(cur_hhmm, prof.early_eight_remind_time):
                    tomorrow_courses = sched.get_courses_for_date(
                        tomorrow, now=now, tz_name=self.timezone
                    )
                    thresh_time = parse_hhmm(prof.early_eight_threshold or "08:00")
                    early_courses = [
                        tc
                        for tc in tomorrow_courses
                        if parse_hhmm(tc.start_time) <= thresh_time
                    ]
                    # 无论明天是否有早八，标记今日已检查，若有早八则发送提醒
                    prof.last_early_eight_remind_date = today_str
                    self.storage.save_profiles()

                    if early_courses:
                        first_ec = early_courses[0]
                        early_lines = [
                            f"🌅 【早八提醒】{display_name}，你明天（{tomorrow.strftime('%m月%d日')} {tomorrow_weekday_str}）早上 {first_ec.start_time} 有早八课《{first_ec.course_name}》！今晚记得定好闹钟、早点休息哦~",
                            f"📚 课程：{first_ec.course_name} ({first_ec.node_range_str})",
                            f"🕒 时间：{first_ec.start_time} - {first_ec.end_time}",
                            f"📍 教室：{first_ec.room or '未指定'}",
                        ]
                        if first_ec.teacher:
                            early_lines.append(f"👤 教师：{first_ec.teacher}")
                        if len(tomorrow_courses) > 1:
                            early_lines.append(
                                f"📊 明日全天共有 {len(tomorrow_courses)} 门课程。"
                            )
                        early_text = "\n".join(early_lines)
                        target_umo = prof.early_eight_umo or prof.reminder_umo or prof.user_umo
                        sent = await self._send_proactive_message(
                            target_umo=target_umo,
                            text=early_text,
                            image_path=None,
                            prof=prof,
                            mention_user_id=prof.user_id,
                        )
                        triggered_events.append(
                            {
                                "type": "early_eight_reminder",
                                "user_id": prof.user_id,
                                "course_name": first_ec.course_name,
                                "umo": target_umo,
                                "text": early_text,
                                "mention_user_id": prof.user_id,
                                "sent": sent,
                            }
                        )

            # -----------------------------------------------------------------
            # 4. 周日自动推送下周上课情况（样式仿照本周上课排行，生成图片推送）
            # -----------------------------------------------------------------
            if (
                today.isoweekday() == 7
                and prof.sunday_push_enabled
                and prof.sunday_push_time
                and prof.last_sunday_push_date != today_str
            ):
                if self._is_time_reached(cur_hhmm, prof.sunday_push_time):
                    prof.last_sunday_push_date = today_str
                    self.storage.save_profiles()

                    target_gid = prof.sunday_push_group_id or (
                        prof.groups[0] if prof.groups else ""
                    )
                    target_umo = prof.sunday_push_umo or (
                        prof.group_umos.get(target_gid, "") if target_gid else ""
                    ) or prof.user_umo

                    dedup_target = target_gid or target_umo or prof.user_id
                    if dedup_target in pushed_sunday_targets:
                        continue
                    pushed_sunday_targets.add(dedup_target)

                    # 将同一群内其他开启了周日推送的成员也标记为今日已推送，避免重复发图
                    if target_gid:
                        for other_prof, _ in bound_users:
                            if target_gid in other_prof.groups:
                                other_prof.last_sunday_push_date = today_str
                        self.storage.save_profiles()

                    group_members = self.storage.get_group_bound_members(
                        target_gid or None
                    )
                    if not group_members:
                        group_members = [(prof, sched)]

                    members_with_avatars: List[
                        Tuple[UserProfile, ScheduleData, Optional[Path]]
                    ] = []
                    for m_prof, m_sched in group_members:
                        av = await get_or_download_avatar(
                            self.storage, m_prof, appid=appid
                        )
                        members_with_avatars.append((m_prof, m_sched, av))

                    next_monday = today + timedelta(days=1)
                    next_sunday = next_monday + timedelta(days=6)
                    img_path = self.renderer.render_weekly_ranking(
                        members_data=members_with_avatars,
                        group_id=target_gid or None,
                        now=now,
                        tz_name=self.timezone,
                        target_monday=next_monday,
                        title_text="下周群友上课情况排行",
                    )
                    caption = (
                        f"📊 【下周上课情况预告】下周（{next_monday.strftime('%m/%d')} - {next_sunday.strftime('%m/%d')}）群友上课时长与节数排行已生成，新的一周加油！"
                    )
                    sent = await self._send_proactive_message(
                        target_umo=target_umo,
                        text=caption,
                        image_path=img_path,
                        prof=prof,
                    )
                    triggered_events.append(
                        {
                            "type": "sunday_next_week_push",
                            "user_id": prof.user_id,
                            "group_id": target_gid,
                            "umo": target_umo,
                            "image_path": str(img_path),
                            "text": caption,
                            "sent": sent,
                        }
                    )

        return triggered_events

    @staticmethod
    def _is_time_reached(current_hhmm: str, target_hhmm: str, window_mins: int = 20) -> bool:
        """判断当前时间是否到达目标推送时间（在 [target, target + window_mins] 区间内）"""
        try:
            ch, cm = [int(x) for x in current_hhmm.split(":")]
            th, tm = [int(x) for x in target_hhmm.split(":")]
            cur_total = ch * 60 + cm
            tgt_total = th * 60 + tm
            return 0 <= (cur_total - tgt_total) <= window_mins
        except Exception:
            return current_hhmm == target_hhmm

    async def _send_proactive_message(
        self,
        target_umo: str,
        text: str,
        image_path: Optional[Path] = None,
        prof: Optional[UserProfile] = None,
        mention_user_id: Optional[str] = None,
    ) -> bool:
        """
        向指定会话发送主动消息（深度适配 QQ 官方机器人 @某人 与 OneBot）：
        - 在 QQ 官方机器人 (qq_official / qqofficial) 下，通过在纯文本开头拼接 `<@{member_openid}> ` 实现原生高亮 @成员
        - 在 OneBot (aiocqhttp) 下，通过 Comp.At(qq=user_id) 组件实现 @成员
        """
        if not self.context or not hasattr(self.context, "send_message"):
            return False

        chain = MessageChain()
        final_text = text or ""

        if mention_user_id:
            uid_str = str(mention_user_id).strip()
            is_qq_official = (
                "qq_official" in (target_umo or "").lower()
                or "qqofficial" in (target_umo or "").lower()
                or not uid_str.isdigit()
            )
            if is_qq_official:
                # 腾讯 QQ 官方机器人群聊标准 @成员 语法：<@member_openid>
                at_tag = f"<@{uid_str}>"
                if at_tag not in final_text:
                    final_text = f"{at_tag} {final_text}".strip()
            else:
                try:
                    chain.chain.append(Comp.At(qq=uid_str))
                except Exception:
                    pass

        if final_text:
            chain.message(final_text)
        if image_path and Path(image_path).exists():
            chain.file_image(str(image_path))

        candidate_umos: List[str] = []
        if target_umo:
            candidate_umos.append(target_umo)
            # 兼容 QQ 官方机器人隔离会话 UMO 回退为标准群聊 UMO
            if "GroupMessage:" in target_umo:
                prefix, tail = target_umo.split("GroupMessage:", 1)
                if prof and prof.user_id and tail.startswith(f"{prof.user_id}_"):
                    group_only_umo = f"{prefix}GroupMessage:{tail[len(prof.user_id) + 1:]}"
                    candidate_umos.append(group_only_umo)

        if prof:
            for g_umo in prof.group_umos.values():
                if g_umo and g_umo not in candidate_umos:
                    candidate_umos.append(g_umo)

        for umo in candidate_umos:
            try:
                await self.context.send_message(umo, chain)
                return True
            except Exception as e:
                logger.warning(
                    f"[astrbot_plugin_schedule] 向 UMO ({umo}) 发送主动消息失败: {e}"
                )
        return False
