from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import UserProfile
from .storage import StorageManager, safe_filename


def extract_qq_official_appid(event: Any = None, context: Any = None, config_appid: str = "") -> str:
    """自动从插件配置、event.bot 或 context.platform_manager 中探测 QQ 官方机器人 AppID"""
    if config_appid and str(config_appid).strip():
        return str(config_appid).strip()

    # 1. 从 event.bot 探测 (botpy Client)
    bot = getattr(event, "bot", None) if event is not None else None
    if bot is not None:
        for attr_path in [
            ("appid",),
            ("_http", "appid"),
            ("api", "_http", "appid"),
            ("robot", "id"),
        ]:
            cur = bot
            for attr in attr_path:
                cur = getattr(cur, attr, None)
                if cur is None and isinstance(cur, dict):
                    cur = cur.get(attr)
            if cur and str(cur).strip():
                return str(cur).strip()

    # 2. 从 context.platform_manager.platform_insts 探测
    if context is not None:
        pm = getattr(context, "platform_manager", None)
        insts = getattr(pm, "platform_insts", []) if pm else []
        for inst in insts:
            cfg = getattr(inst, "config", None)
            if isinstance(cfg, dict):
                appid = cfg.get("appid") or cfg.get("app_id")
                if appid and str(appid).strip():
                    return str(appid).strip()
            client = getattr(inst, "client", None) or getattr(inst, "bot", None)
            if client is not None:
                appid = getattr(client, "appid", None) or getattr(
                    getattr(client, "_http", None), "appid", None
                )
                if appid and str(appid).strip():
                    return str(appid).strip()

    return ""


def extract_event_identity(event: Any) -> Dict[str, str]:
    """
    从 AstrMessageEvent 中提取跨平台（特别是 qq_official 与 aiocqhttp）身份信息：
    返回 dict:
      - platform: 平台名称
      - user_id: 发送者唯一 ID (OpenID 或 QQ号)
      - group_id: 群聊唯一 ID (group_openid / channel_id / group_id)
      - nickname: 尝试提取到的群名片或用户昵称
      - avatar_url: 尝试从事件对象提取到的直接头像链接
      - umo: unified_msg_origin
      - qq_number: 若可识别出纯数字 QQ 号则返回
    """
    platform = ""
    try:
        platform = str(event.get_platform_name() or "")
    except Exception:
        platform = str(getattr(event, "platform_name", "") or "")

    user_id = ""
    try:
        user_id = str(event.get_sender_id() or "").strip()
    except Exception:
        pass

    group_id = ""
    try:
        group_id = str(event.get_group_id() or "").strip()
    except Exception:
        pass

    nickname = ""
    try:
        nickname = str(event.get_sender_name() or "").strip()
    except Exception:
        pass

    umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
    avatar_url = ""
    qq_number = user_id if (user_id.isdigit() and 5 <= len(user_id) <= 12) else ""

    msg_obj = getattr(event, "message_obj", None)
    if msg_obj is not None:
        if not group_id:
            gid = getattr(msg_obj, "group_id", "")
            if gid:
                group_id = str(gid).strip()

        sender = getattr(msg_obj, "sender", None)
        if sender is not None:
            if isinstance(sender, dict):
                card = str(sender.get("card") or "").strip()
                nick = str(sender.get("nickname") or sender.get("username") or "").strip()
                av = str(sender.get("avatar") or "").strip()
                uid = str(sender.get("user_id") or sender.get("id") or "").strip()
            else:
                card = str(getattr(sender, "card", "") or "").strip()
                nick = str(
                    getattr(sender, "nickname", "")
                    or getattr(sender, "username", "")
                    or ""
                ).strip()
                av = str(getattr(sender, "avatar", "") or "").strip()
                uid = str(
                    getattr(sender, "user_id", "") or getattr(sender, "id", "") or ""
                ).strip()

            if card:
                nickname = card
            elif nick and (not nickname or nickname == user_id):
                nickname = nick
            if av:
                avatar_url = av
            if uid and not user_id:
                user_id = uid
            if uid.isdigit() and 5 <= len(uid) <= 12 and not qq_number:
                qq_number = uid

        # 检查底层 raw_message (例如 botpy 的 GroupMessage / Message / C2CMessage)
        raw_msg = getattr(msg_obj, "raw_message", None)
        if raw_msg is not None:
            if isinstance(raw_msg, dict):
                if not group_id:
                    group_id = str(
                        raw_msg.get("group_openid")
                        or raw_msg.get("group_id")
                        or raw_msg.get("guild_id")
                        or raw_msg.get("channel_id")
                        or ""
                    ).strip()
                author = raw_msg.get("author") or {}
                member = raw_msg.get("member") or {}
                if isinstance(author, dict):
                    if not user_id:
                        user_id = str(
                            author.get("member_openid")
                            or author.get("user_openid")
                            or author.get("id")
                            or ""
                        ).strip()
                    if not avatar_url and author.get("avatar"):
                        avatar_url = str(author.get("avatar")).strip()
                    uname = str(author.get("username") or "").strip()
                    if uname and (not nickname or nickname == user_id):
                        nickname = uname
                if isinstance(member, dict):
                    mnick = str(member.get("nick") or "").strip()
                    if mnick:
                        nickname = mnick
                raw_sender = raw_msg.get("sender")
                if isinstance(raw_sender, dict):
                    scard = str(raw_sender.get("card") or "").strip()
                    snick = str(raw_sender.get("nickname") or "").strip()
                    if scard:
                        nickname = scard
                    elif snick and (not nickname or nickname == user_id):
                        nickname = snick
            else:
                if not group_id:
                    for gattr in ("group_openid", "group_id", "guild_id", "channel_id"):
                        gval = getattr(raw_msg, gattr, None)
                        if gval:
                            group_id = str(gval).strip()
                            break
                author = getattr(raw_msg, "author", None)
                if author is not None:
                    if not user_id:
                        for uattr in ("member_openid", "user_openid", "id"):
                            uval = getattr(author, uattr, None)
                            if uval:
                                user_id = str(uval).strip()
                                break
                    av = getattr(author, "avatar", None)
                    if av and not avatar_url:
                        avatar_url = str(av).strip()
                    uname = getattr(author, "username", None)
                    if uname and (not nickname or nickname == user_id):
                        nickname = str(uname).strip()
                member = getattr(raw_msg, "member", None)
                if member is not None:
                    mnick = getattr(member, "nick", None)
                    if mnick:
                        nickname = str(mnick).strip()

    # 如果 UMO 中包含群信息但 group_id 仍为空（例如 qq_official:GroupMessage:xxx）
    if not group_id and umo and "GroupMessage:" in umo:
        tail = umo.split("GroupMessage:", 1)[1].strip()
        if "_" in tail and user_id and tail.startswith(f"{user_id}_"):
            group_id = tail[len(user_id) + 1 :]
        else:
            group_id = tail

    # 标准化头像链接前缀
    if avatar_url.startswith("//"):
        avatar_url = "https:" + avatar_url

    return {
        "platform": platform,
        "user_id": user_id or "unknown_user",
        "group_id": group_id,
        "nickname": nickname,
        "avatar_url": avatar_url,
        "umo": umo,
        "qq_number": qq_number,
    }


async def try_fetch_onebot_group_nickname(event: Any, group_id: str, user_id: str) -> str:
    """如果是 OneBot (aiocqhttp) 平台，尝试调用 get_group_member_info 获取最新群名片"""
    bot = getattr(event, "bot", None)
    if bot is None or not group_id.isdigit() or not user_id.isdigit():
        return ""
    try:
        call_action = getattr(bot, "get_group_member_info", None)
        if callable(call_action):
            info = await call_action(group_id=int(group_id), user_id=int(user_id), no_cache=False)
            if isinstance(info, dict):
                card = str(info.get("card") or "").strip()
                nick = str(info.get("nickname") or "").strip()
                return card or nick
    except Exception:
        pass
    return ""


async def fetch_qq_nickname_by_number(qq_number: str) -> str:
    """通过纯数字 QQ 号查询腾讯公开接口获取 QQ 昵称"""
    if not qq_number or not qq_number.isdigit():
        return ""
    url = f"https://users.qzone.qq.com/fcg-bin/cgi_get_portrait.fcg?uins={qq_number}"

    def _sync_get() -> str:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}, method="GET"
            )
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                raw = resp.read()
                text = raw.decode("gbk", errors="ignore")
                m = re.search(r'\[.*?,.*?,.*?,.*?,.*?,.*?,"(.*?)",', text)
                if m and m.group(1).strip():
                    return m.group(1).strip()
        except Exception:
            pass
        return ""

    return await asyncio.to_thread(_sync_get)


def build_candidate_avatar_urls(
    profile: UserProfile, appid: str = ""
) -> List[str]:
    """根据用户资料构建多级候选头像 URL 列表（兼顾 QQ 官方机器人 OpenID 与普通 QQ 号）"""
    urls: List[str] = []
    if profile.avatar_url and profile.avatar_url.startswith("http"):
        urls.append(profile.avatar_url)

    qq_num = profile.qq_number
    if not qq_num and profile.user_id.isdigit() and 5 <= len(profile.user_id) <= 12:
        qq_num = profile.user_id

    if qq_num and qq_num.isdigit():
        urls.append(f"https://q1.qlogo.cn/g?b=qq&nk={qq_num}&s=640")
        urls.append(f"https://q2.qlogo.cn/headimg_dl?dst_uin={qq_num}&spec=100")

    if appid and profile.user_id:
        urls.append(f"https://thirdqq.qlogo.cn/qqapp/{appid}/{profile.user_id}/100")
        urls.append(f"https://q.qlogo.cn/qqapp/{appid}/{profile.user_id}/100")

    # 去重
    unique: List[str] = []
    for u in urls:
        if u not in unique:
            unique.append(u)
    return unique


async def get_or_download_avatar(
    storage: StorageManager,
    profile: UserProfile,
    appid: str = "",
    cache_ttl: int = 86400,
) -> Optional[Path]:
    """获取或下载用户的 QQ 头像并缓存到本地磁盘，失败时返回 None 由渲染器绘制高颜值默认头像"""
    cache_path = storage.avatars_dir / f"{safe_filename(profile.user_id)}.img"
    if cache_path.exists() and cache_path.stat().st_size > 100:
        age = time.time() - cache_path.stat().st_mtime
        if age < cache_ttl:
            return cache_path

    urls = build_candidate_avatar_urls(profile, appid=appid)
    if not urls:
        return cache_path if cache_path.exists() else None

    for url in urls:
        data = await _download_bytes(url, timeout=6)
        if data and len(data) > 100:
            try:
                cache_path.write_bytes(data)
                return cache_path
            except Exception:
                pass

    return cache_path if (cache_path.exists() and cache_path.stat().st_size > 100) else None


async def _download_bytes(url: str, timeout: int = 8) -> Optional[bytes]:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        import aiohttp  # type: ignore

        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    return await resp.read()
    except ImportError:
        pass
    except Exception:
        pass

    def _sync_dl() -> Optional[bytes]:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except Exception:
            return None

    return await asyncio.to_thread(_sync_dl)


async def extract_schedule_input_from_event(event: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    从用户消息事件中提取课表绑定输入（兼容 QQ 官方机器人与 OneBot）：
    返回 (ics_content_or_none, text_input_or_none)
    支持：
    1. 消息链中的 File 组件 (Comp.File)
    2. QQ 官方机器人 raw_message.attachments 附件中的 .ics 文件
    3. 消息文本中的 .ics 下载链接 (http/https)
    4. 消息文本中直接粘贴的 BEGIN:VCALENDAR 内容
    5. 消息文本中的 WakeUp 课程表分享口令
    """
    msg_str = str(getattr(event, "message_str", "") or "").strip()
    msg_obj = getattr(event, "message_obj", None)

    # 1. 检查消息链中的 File 组件
    chain = getattr(msg_obj, "message", []) if msg_obj else []
    for seg in chain or []:
        seg_type = type(seg).__name__.lower()
        if seg_type == "file" or hasattr(seg, "file") and seg_type != "image":
            file_url_or_path = (
                getattr(seg, "url", None)
                or getattr(seg, "file", None)
                or getattr(seg, "path", None)
            )
            # 如果有异步 get_file 方法
            get_file_fn = getattr(seg, "get_file", None)
            if callable(get_file_fn):
                try:
                    res = await get_file_fn()
                    if res:
                        file_url_or_path = res
                except Exception:
                    pass
            if file_url_or_path:
                s_val = str(file_url_or_path).strip()
                if s_val.startswith(("http://", "https://", "//")):
                    if s_val.startswith("//"):
                        s_val = "https:" + s_val
                    raw_bytes = await _download_bytes(s_val, timeout=12)
                    if raw_bytes:
                        text = raw_bytes.decode("utf-8", errors="replace")
                        if "BEGIN:VCALENDAR" in text.upper():
                            return text, None
                else:
                    p = Path(s_val)
                    if p.exists() and p.is_file():
                        text = p.read_text(encoding="utf-8", errors="replace")
                        if "BEGIN:VCALENDAR" in text.upper():
                            return text, None

    # 2. 检查 QQ 官方机器人 raw_message.attachments
    raw_msg = getattr(msg_obj, "raw_message", None) if msg_obj else None
    attachments = None
    if isinstance(raw_msg, dict):
        attachments = raw_msg.get("attachments")
    elif raw_msg is not None:
        attachments = getattr(raw_msg, "attachments", None)

    if attachments and isinstance(attachments, (list, tuple)):
        for att in attachments:
            if isinstance(att, dict):
                att_url = str(att.get("url") or "").strip()
                filename = str(att.get("filename") or "").lower()
            else:
                att_url = str(getattr(att, "url", "") or "").strip()
                filename = str(getattr(att, "filename", "") or "").lower()
            if att_url:
                if att_url.startswith("//"):
                    att_url = "https:" + att_url
                elif not att_url.startswith("http"):
                    att_url = "https://" + att_url.lstrip("/")
                raw_bytes = await _download_bytes(att_url, timeout=12)
                if raw_bytes:
                    text = raw_bytes.decode("utf-8", errors="replace")
                    if "BEGIN:VCALENDAR" in text.upper() or filename.endswith(".ics"):
                        return text, None

    # 3. 检查文本中是否直接包含 BEGIN:VCALENDAR
    if "BEGIN:VCALENDAR" in msg_str.upper():
        return msg_str, None

    # 4. 检查文本中是否包含 .ics 下载链接或本地路径
    m_url = re.search(r"(https?://[^\s]+)", msg_str)
    if m_url:
        candidate_url = m_url.group(1).strip()
        if ".ics" in candidate_url.lower() or "calendar" in candidate_url.lower():
            raw_bytes = await _download_bytes(candidate_url, timeout=12)
            if raw_bytes:
                text = raw_bytes.decode("utf-8", errors="replace")
                if "BEGIN:VCALENDAR" in text.upper():
                    return text, None

    return None, msg_str
