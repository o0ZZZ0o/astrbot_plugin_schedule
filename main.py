from __future__ import annotations

import asyncio
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

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
        def __init__(self, chain: Optional[ List[Any] ] = None):
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


@register(
    "astrbot_plugin_schedule",
    "AstrBot Developer",
    "为AstrBot设计的课程表插件，支持.ics与WakeUp口令绑定、三种精美渲染样式、每日推送与课前提醒，深度适配QQ官方机器人",
    "1.1.0",
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

        # 待处理交互会话备份字典: key = (group_id, user_id), val = {"action": ..., "expire": timestamp, "umo": ...}
        self._pending_sessions: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # 启动后台定时任务（每日课表推送 & 开课前提醒）
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
    # 身份同步与消息构造辅助方法（深度适配 QQ 官方机器人）
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

        # 如果在 OneBot 群聊且未拿到群名片，尝试调用 API 获取群昵称
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

        # 如果仍然没有可读昵称但有数字 QQ 号，自动通过腾讯接口补全昵称
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
        # 去除前导 @机器人 或 /
        cleaned = re.sub(r"^@\S+\s*", "", raw).strip()
        for cname in sorted(cmd_names, key=len, reverse=True):
            pat = rf"^[/!！#]?{re.escape(cname)}(?:[\s：:]+|$)(.*)$"
            m = re.match(pat, cleaned, flags=re.DOTALL | re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return ""

    # =========================================================================
    # 1. /绑定课表
    # =========================================================================
    @filter.command("绑定课表")
    async def cmd_bind_schedule(self, event: AstrMessageEvent):
        """在群聊或私聊中发送此命令，通过 .ics 文件或 WakeUp 分享口令绑定个人课表"""
        prof, ident = await self._sync_user_from_event(event)
        user_id = ident["user_id"]
        group_id = ident["group_id"]

        # 检查是否在同一条消息中直接附带了 .ics 文件或 WakeUp 口令
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

        # 进入多轮交互等待
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
            }
            return

        target_uid = user_id

        @session_waiter(timeout=self.session_timeout, record_history_chains=False)
        async def bind_waiter(controller: SessionController, wait_event: AstrMessageEvent):
            w_ident = extract_event_identity(wait_event)
            if w_ident["user_id"] != target_uid:
                # 群内其他成员发言，保持当前用户的等待会话不中断
                controller.keep(timeout=self.session_timeout, reset_timeout=False)
                return

            text = str(getattr(wait_event, "message_str", "") or "").strip()
            if text in ("取消", "/取消", "退出", "/退出"):
                await wait_event.send(wait_event.plain_result("✅ 已取消课表绑定操作。"))
                controller.stop()
                return

            w_ics, w_text = await extract_schedule_input_from_event(wait_event)
            # 若用户在等待期间重新发了 /绑定课表 xxx，剥离前缀
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
        """解析并保存用户上传的 .ics 内容或 WakeUp 分享口令"""
        # 检查文本中是否顺便附带了 昵称:xxx 或 QQ:123456
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
        # 预热下载头像
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
            "👉 常用指令：/查看课表、/今日课表、/明日课表、/本周课表、/群友在上什么课、/本周上课排行",
        ]
        if prof._is_raw_id(prof.nickname):
            lines.append(
                "💡 提示：检测到当前平台未返回你的群昵称，可发送「/设置昵称 你的群昵称 你的QQ号」以在课表和排行榜上展示你的群昵称与QQ头像！"
            )
        return True, "\n".join(lines)

    # =========================================================================
    # 2. /查看课表 （显示自己今天接下来要上的课程，输出图片）
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

    # =========================================================================
    # 3. /今日课表 （输出今日完整课表图片）
    # =========================================================================
    @filter.command("今日课表")
    async def cmd_today_schedule(self, event: AstrMessageEvent):
        """输出今日课表图片"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        if not sched:
            yield event.plain_result(
                "⚠️ 你还没有绑定课表哦！请先发送 /绑定课表 进行绑定。"
            )
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

    # =========================================================================
    # 4. /明日课表 （输出明日课表图片）
    # =========================================================================
    @filter.command("明日课表", alias={"查看明日课表"})
    async def cmd_tomorrow_schedule(self, event: AstrMessageEvent):
        """输出明日课表图片"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        if not sched:
            yield event.plain_result(
                "⚠️ 你还没有绑定课表哦！请先发送 /绑定课表 进行绑定。"
            )
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

    # =========================================================================
    # 5. /本周课表 （输出本周课表图片）
    # =========================================================================
    @filter.command("本周课表")
    async def cmd_this_week_schedule(self, event: AstrMessageEvent):
        """输出本周课表图片"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        if not sched:
            yield event.plain_result(
                "⚠️ 你还没有绑定课表哦！请先发送 /绑定课表 进行绑定。"
            )
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

    # =========================================================================
    # 6. /下周课表 （输出下周课表图片）
    # =========================================================================
    @filter.command("下周课表")
    async def cmd_next_week_schedule(self, event: AstrMessageEvent):
        """输出下周课表图片"""
        prof, ident = await self._sync_user_from_event(event)
        group_id = ident["group_id"]
        sched = self.storage.get_schedule(prof.user_id)
        if not sched:
            yield event.plain_result(
                "⚠️ 你还没有绑定课表哦！请先发送 /绑定课表 进行绑定。"
            )
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
    # 7. /群友在上什么课 （显示群里所有成员当前正在上或下一节要上的课程）
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

    # =========================================================================
    # 8. /群友明天上什么课 （显示群里所有成员明天要上的课程）
    # =========================================================================
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

    # =========================================================================
    # 9. /本周上课排行 （显示本周群友上课时长和节数的排行榜）
    # =========================================================================
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
        )
        yield event.image_result(str(img_path))

    # =========================================================================
    # 10. /设置每日推送 （回复 "开启 HH:MM" 如 "开启 07:00"，或回复 "关闭" 禁用）
    # =========================================================================
    @filter.command("设置每日推送")
    async def cmd_set_daily_push(self, event: AstrMessageEvent):
        """配置每日课表自动推送（回复"开启 HH:MM"如"开启 07:00"，或回复"关闭"禁用）"""
        prof, ident = await self._sync_user_from_event(event)
        user_id = ident["user_id"]
        group_id = ident["group_id"]
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(event, ["设置每日推送"])
        if inline_arg:
            ok, msg = self._apply_daily_push_setting(prof, inline_arg, umo)
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

        if not HAS_ASTRBOT:
            self._pending_sessions[(group_id, user_id)] = {
                "action": "set_daily_push",
                "expire": now_in_tz(self.timezone).timestamp() + self.session_timeout,
                "umo": umo,
            }
            return

        target_uid = user_id

        @session_waiter(timeout=self.session_timeout, record_history_chains=False)
        async def push_waiter(controller: SessionController, wait_event: AstrMessageEvent):
            w_ident = extract_event_identity(wait_event)
            if w_ident["user_id"] != target_uid:
                controller.keep(timeout=self.session_timeout, reset_timeout=False)
                return

            text = str(getattr(wait_event, "message_str", "") or "").strip()
            text = re.sub(r"^@\S+\s*", "", text).strip()
            if text in ("取消", "/取消", "退出", "/退出"):
                await wait_event.send(wait_event.plain_result("✅ 已取消每日推送设置。"))
                controller.stop()
                return

            w_arg = self._extract_command_arg(wait_event, ["设置每日推送"]) or text
            w_prof, _ = await self._sync_user_from_event(wait_event)
            ok, reply = self._apply_daily_push_setting(
                w_prof, w_arg, w_ident["umo"] or umo
            )
            await wait_event.send(wait_event.plain_result(reply))
            if ok:
                controller.stop()
            else:
                controller.keep(timeout=self.session_timeout, reset_timeout=True)

        try:
            await push_waiter(event)
        except TimeoutError:
            yield event.plain_result("⏰ 设置每日推送等待已超时，请重新发送 /设置每日推送。")
        finally:
            try:
                event.stop_event()
            except Exception:
                pass

    def _apply_daily_push_setting(
        self, prof: UserProfile, user_reply: str, umo: str
    ) -> Tuple[bool, str]:
        cleaned = user_reply.strip().lstrip("/")
        if cleaned in ("关闭", "禁用", "off", "OFF", "false", "0"):
            prof.daily_push_enabled = False
            self.storage.save_profiles()
            return True, "✅ 已关闭每日课表自动推送。"

        m = re.match(
            r"^(?:开启|启用|打开|on|ON)?\s*(\d{1,2})\s*[:：点时]\s*(\d{1,2})\s*分?$",
            cleaned,
        )
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                hhmm = f"{hh:02d}:{mm:02d}"
                prof.daily_push_enabled = True
                prof.daily_push_time = hhmm
                if umo:
                    prof.daily_push_umo = umo
                self.storage.save_profiles()
                return (
                    True,
                    f"✅ 已开启每日课表自动推送！每天 {hhmm} 将自动为你推送当日课表（请确保机器人拥有主动消息推送权限）。",
                )
            return False, "⚠️ 时间范围无效，小时需在 00-23 之间，分钟需在 00-59 之间（例如：开启 07:00）。"

        return (
            False,
            "⚠️ 格式不正确！请回复“开启 HH:MM”（例如“开启 07:00”），或回复“关闭”禁用推送。",
        )

    # =========================================================================
    # 11. /设置提醒时间 （配置开课前提醒的提前时间，回复分钟数 1-120）
    # =========================================================================
    @filter.command("设置提醒时间")
    async def cmd_set_reminder_time(self, event: AstrMessageEvent):
        """配置开课前提醒的提前时间（回复分钟数，范围 1-120）"""
        prof, ident = await self._sync_user_from_event(event)
        user_id = ident["user_id"]
        group_id = ident["group_id"]
        umo = ident["umo"]

        inline_arg = self._extract_command_arg(event, ["设置提醒时间"])
        if inline_arg:
            ok, msg = self._apply_reminder_setting(prof, inline_arg, umo)
            yield event.plain_result(msg)
            return

        cur_rem = (
            f"开课前 {prof.reminder_minutes} 分钟提醒"
            if prof.reminder_minutes > 0
            else "未开启"
        )
        yield event.plain_result(
            f"🔔 当前开课前提醒状态：{cur_rem}\n"
            f"请回复提前提醒的分钟数（范围 1-120，如回复“15”表示课前 15 分钟提醒；回复“关闭”或“0”可禁用提醒）。"
        )

        if not HAS_ASTRBOT:
            self._pending_sessions[(group_id, user_id)] = {
                "action": "set_reminder_time",
                "expire": now_in_tz(self.timezone).timestamp() + self.session_timeout,
                "umo": umo,
            }
            return

        target_uid = user_id

        @session_waiter(timeout=self.session_timeout, record_history_chains=False)
        async def reminder_waiter(controller: SessionController, wait_event: AstrMessageEvent):
            w_ident = extract_event_identity(wait_event)
            if w_ident["user_id"] != target_uid:
                controller.keep(timeout=self.session_timeout, reset_timeout=False)
                return

            text = str(getattr(wait_event, "message_str", "") or "").strip()
            text = re.sub(r"^@\S+\s*", "", text).strip()
            if text in ("取消", "/取消", "退出", "/退出"):
                await wait_event.send(wait_event.plain_result("✅ 已取消提醒时间设置。"))
                controller.stop()
                return

            w_arg = self._extract_command_arg(wait_event, ["设置提醒时间"]) or text
            w_prof, _ = await self._sync_user_from_event(wait_event)
            ok, reply = self._apply_reminder_setting(
                w_prof, w_arg, w_ident["umo"] or umo
            )
            await wait_event.send(wait_event.plain_result(reply))
            if ok:
                controller.stop()
            else:
                controller.keep(timeout=self.session_timeout, reset_timeout=True)

        try:
            await reminder_waiter(event)
        except TimeoutError:
            yield event.plain_result("⏰ 设置提醒时间等待已超时，请重新发送 /设置提醒时间。")
        finally:
            try:
                event.stop_event()
            except Exception:
                pass

    def _apply_reminder_setting(
        self, prof: UserProfile, user_reply: str, umo: str
    ) -> Tuple[bool, str]:
        cleaned = user_reply.strip().lstrip("/")
        if cleaned in ("关闭", "禁用", "0", "off", "OFF", "false"):
            prof.reminder_minutes = 0
            self.storage.save_profiles()
            return True, "✅ 已关闭开课前提醒功能。"

        m = re.match(r"^(\d{1,3})\s*(?:分钟|min|m)?$", cleaned, re.IGNORECASE)
        if m:
            mins = int(m.group(1))
            if 1 <= mins <= 120:
                prof.reminder_minutes = mins
                if umo:
                    prof.reminder_umo = umo
                self.storage.save_profiles()
                return (
                    True,
                    f"✅ 已将开课前提醒时间设置为：提前 {mins} 分钟！",
                )
            return False, "⚠️ 提醒时间超出范围！请输入 1 到 120 之间的整数分钟数（或回复“关闭”禁用）。"

        return False, "⚠️ 格式不正确！请回复 1-120 之间的分钟数字（例如“15”），或回复“关闭”禁用提醒。"

    # =========================================================================
    # 12. /查看设置 （查看当前配置）
    # =========================================================================
    @filter.command("查看设置")
    async def cmd_view_settings(self, event: AstrMessageEvent):
        """查看当前配置"""
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
            else "未开启（发送 /设置每日推送 可配置）"
        )
        reminder_status = (
            f"已开启（开课前 {prof.reminder_minutes} 分钟提醒）"
            if prof.reminder_minutes > 0
            else "未开启（发送 /设置提醒时间 可配置）"
        )
        qq_status = prof.qq_number if prof.qq_number else "未关联（可发送 /设置昵称 群昵称 QQ号 关联）"

        lines = [
            "⚙️ 【个人课表与通知设置】",
            f"👤 显示群昵称：{display_name}",
            f"🐧 关联QQ号：{qq_status}",
            f"📚 课表状态：{sched_status}",
            f"⏰ 每日推送：{push_status}",
            f"🔔 课前提醒：{reminder_status}",
            f"🌍 当前时区：{self.timezone} ({now.strftime('%Y-%m-%d %H:%M')})",
        ]
        yield event.plain_result("\n".join(lines))

    # =========================================================================
    # 辅助命令：/设置昵称、/解绑课表、/课表帮助（专为 QQ 官方机器人体验增强）
    # =========================================================================
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
            # 用户只输入了 QQ 号，尝试自动拉取 QQ 昵称
            new_qq = new_nick
            fetched = await fetch_qq_nickname_by_number(new_qq)
            new_nick = fetched or prof.nickname or f"群友({new_qq[-4:]})"

        prof = self.storage.set_custom_nickname(
            user_id=prof.user_id,
            nickname=new_nick,
            group_id=group_id or None,
            qq_number=new_qq or None,
        )
        # 刷新头像缓存
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
            "📖 【AstrBot 课程表插件指令列表】",
            "1. /绑定课表 ：发送 .ics 文件或 WakeUp 分享口令绑定课表",
            "2. /查看课表 ：显示自己今天接下来要上的课程（图片）",
            "3. /今日课表 ：输出今日完整课表图片",
            "4. /明日课表 ：输出明日课表图片",
            "5. /本周课表 ：输出本周课表网格图片",
            "6. /下周课表 ：输出下周课表网格图片",
            "7. /群友在上什么课 ：查看群内所有已绑定成员当前上课/空闲状态",
            "8. /群友明天上什么课 ：查看群内所有成员明天的课程安排",
            "9. /本周上课排行 ：查看本周群友上课时长与节数排行榜",
            "10. /设置每日推送 ：配置每日课表推送（回复“开启 07:00”或“关闭”）",
            "11. /设置提醒时间 ：配置开课前提醒分钟数（回复 1-120 或“关闭”）",
            "12. /查看设置 ：查看当前个人课表与提醒配置",
            "13. /设置昵称 <群昵称> [QQ号] ：自定义群昵称与关联QQ头像（适配QQ官方机器人）",
            "14. /解绑课表 ：清除已绑定的个人课表",
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
        # 如果发送的是其他斜杠指令则不拦截
        if text.startswith("/") and not any(
            text.startswith(p) for p in ("/开启", "/关闭", "/取消")
        ):
            return

        action = session_info["action"]
        prof, _ = await self._sync_user_from_event(event)

        if text in ("取消", "/取消", "退出", "/退出"):
            self._pending_sessions.pop(key, None)
            self._pending_sessions.pop(("", ident["user_id"]), None)
            yield event.plain_result("✅ 已取消当前操作。")
            return

        if action == "bind_schedule":
            ics_c, txt_c = await extract_schedule_input_from_event(event)
            ok, msg = await self._process_bind_input(
                event, prof, ident["group_id"], ics_c, txt_c or text
            )
            if ok:
                self._pending_sessions.pop(key, None)
                self._pending_sessions.pop(("", ident["user_id"]), None)
            yield event.plain_result(msg)
        elif action == "set_daily_push":
            ok, msg = self._apply_daily_push_setting(
                prof, text, ident["umo"] or session_info.get("umo", "")
            )
            if ok:
                self._pending_sessions.pop(key, None)
                self._pending_sessions.pop(("", ident["user_id"]), None)
            yield event.plain_result(msg)
        elif action == "set_reminder_time":
            ok, msg = self._apply_reminder_setting(
                prof, text, ident["umo"] or session_info.get("umo", "")
            )
            if ok:
                self._pending_sessions.pop(key, None)
                self._pending_sessions.pop(("", ident["user_id"]), None)
            yield event.plain_result(msg)

    # =========================================================================
    # 定时任务：每日课表自动推送 & 开课前提醒（适配 QQ 官方机器人主动推送）
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
        """执行一次每日推送与课前提醒检查，返回本次触发的推送记录（便于测试与日志审计）"""
        if now is None:
            now = now_in_tz(self.timezone)
        today = now.date()
        today_str = today.isoformat()
        cur_hhmm = now.strftime("%H:%M")

        triggered_events: List[Dict[str, Any]] = []
        bound_users = self.storage.get_all_bound_users()
        appid = self._get_appid()

        for prof, sched in bound_users:
            day_courses = sched.get_courses_for_date(
                today, now=now, tz_name=self.timezone
            )
            display_name = prof.get_display_name()

            # 1. 检查每日定时推送
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

            # 2. 检查开课前提醒
            if 1 <= prof.reminder_minutes <= 120 and day_courses:
                # 清理非今日的已提醒缓存
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
                        )
                        triggered_events.append(
                            {
                                "type": "class_reminder",
                                "user_id": prof.user_id,
                                "course_name": c.course_name,
                                "umo": target_umo,
                                "text": rem_text,
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
    ) -> bool:
        """
        向指定会话发送主动消息（兼容 QQ 官方机器人与 OneBot）：
        - 构造不携带过期 msg_id 的纯净 MessageChain
        - 若主 UMO 发送失败，自动尝试群聊标准 UMO 回退
        """
        if not self.context or not hasattr(self.context, "send_message"):
            return False

        chain = MessageChain()
        if text:
            chain.message(text)
        if image_path and Path(image_path).exists():
            chain.file_image(str(image_path))

        candidate_umos: List[str] = []
        if target_umo:
            candidate_umos.append(target_umo)
            # 针对 QQ 官方机器人会话隔离格式的兼容回退:
            # 如 qq_official:GroupMessage:member_openid_group_openid -> qq_official:GroupMessage:group_openid
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
