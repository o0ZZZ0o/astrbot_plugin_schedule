from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import ScheduleData, UserProfile


def safe_filename(raw_id: str) -> str:
    """将可能包含特殊字符或较长的 OpenID 转换为安全文件名"""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(raw_id))
    if len(cleaned) > 64:
        digest = hashlib.md5(str(raw_id).encode("utf-8")).hexdigest()[:12]
        return f"{cleaned[:40]}_{digest}"
    return cleaned or "default_user"


class StorageManager:
    """插件持久化存储管理"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.schedules_dir = self.data_dir / "schedules"
        self.avatars_dir = self.data_dir / "avatars"
        self.renders_dir = self.data_dir / "renders"
        self.profiles_file = self.data_dir / "profiles.json"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.schedules_dir.mkdir(parents=True, exist_ok=True)
        self.avatars_dir.mkdir(parents=True, exist_ok=True)
        self.renders_dir.mkdir(parents=True, exist_ok=True)

        self._profiles: Dict[str, UserProfile] = {}
        self._schedules_cache: Dict[str, ScheduleData] = {}
        self._load_profiles()

    def _load_profiles(self) -> None:
        if not self.profiles_file.exists():
            self._profiles = {}
            return
        try:
            raw = json.loads(self.profiles_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._profiles = {
                    uid: UserProfile.from_dict(pdata)
                    for uid, pdata in raw.items()
                    if isinstance(pdata, dict)
                }
        except Exception:
            self._profiles = {}

    def save_profiles(self) -> None:
        data = {uid: prof.to_dict() for uid, prof in self._profiles.items()}
        tmp_file = self.profiles_file.with_suffix(".json.tmp")
        tmp_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_file.replace(self.profiles_file)

    def get_profile(self, user_id: str) -> UserProfile:
        user_id = str(user_id)
        if user_id not in self._profiles:
            prof = UserProfile(user_id=user_id)
            if user_id.isdigit() and 5 <= len(user_id) <= 12:
                prof.qq_number = user_id
            self._profiles[user_id] = prof
            self.save_profiles()
        return self._profiles[user_id]

    def update_user_context(
        self,
        user_id: str,
        group_id: Optional[str] = None,
        nickname: Optional[str] = None,
        umo: Optional[str] = None,
        avatar_url: Optional[str] = None,
        qq_number: Optional[str] = None,
    ) -> UserProfile:
        prof = self.get_profile(user_id)
        changed = False

        if qq_number and qq_number.isdigit() and prof.qq_number != qq_number:
            prof.qq_number = qq_number
            changed = True

        if group_id:
            group_id = str(group_id)
            if group_id not in prof.groups:
                prof.groups.append(group_id)
                changed = True
            if umo and prof.group_umos.get(group_id) != umo:
                prof.group_umos[group_id] = umo
                changed = True

        if umo and prof.user_umo != umo:
            prof.user_umo = umo
            changed = True

        if avatar_url and prof.avatar_url != avatar_url:
            prof.avatar_url = avatar_url
            changed = True

        if nickname:
            nick_clean = nickname.strip()
            # 避免用原始 OpenID 或空字符串覆盖已有有效昵称
            if nick_clean and not prof._is_raw_id(nick_clean):
                if group_id:
                    # 如果群内之前没有手工设置昵称，或者新昵称不是纯数字QQ号，则更新
                    existing_gn = prof.group_nicknames.get(group_id, "")
                    if not existing_gn or not nick_clean.isdigit():
                        if existing_gn != nick_clean:
                            prof.group_nicknames[group_id] = nick_clean
                            changed = True
                if not prof.nickname or not nick_clean.isdigit():
                    if prof.nickname != nick_clean:
                        prof.nickname = nick_clean
                        changed = True

        if changed:
            self.save_profiles()
        return prof

    def set_custom_nickname(
        self,
        user_id: str,
        nickname: str,
        group_id: Optional[str] = None,
        qq_number: Optional[str] = None,
    ) -> UserProfile:
        prof = self.get_profile(user_id)
        nick_clean = nickname.strip()
        if nick_clean:
            prof.nickname = nick_clean
            if group_id:
                prof.group_nicknames[str(group_id)] = nick_clean
            for gid in prof.groups:
                prof.group_nicknames[gid] = nick_clean
        if qq_number and qq_number.strip().isdigit():
            prof.qq_number = qq_number.strip()
        self.save_profiles()
        return prof

    def save_schedule(self, user_id: str, schedule: ScheduleData) -> Path:
        user_id = str(user_id)
        self._schedules_cache[user_id] = schedule
        file_path = self.schedules_dir / f"{safe_filename(user_id)}.json"
        file_path.write_text(
            json.dumps(schedule.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 确保 profile 存在
        self.get_profile(user_id)
        return file_path

    def get_schedule(self, user_id: str) -> Optional[ScheduleData]:
        user_id = str(user_id)
        if user_id in self._schedules_cache:
            return self._schedules_cache[user_id]
        file_path = self.schedules_dir / f"{safe_filename(user_id)}.json"
        if not file_path.exists():
            return None
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            sched = ScheduleData.from_dict(raw)
            self._schedules_cache[user_id] = sched
            return sched
        except Exception:
            return None

    def delete_schedule(self, user_id: str) -> bool:
        user_id = str(user_id)
        self._schedules_cache.pop(user_id, None)
        file_path = self.schedules_dir / f"{safe_filename(user_id)}.json"
        if file_path.exists():
            file_path.unlink()
            return True
        return False

    def get_group_bound_members(
        self, group_id: Optional[str]
    ) -> List[Tuple[UserProfile, ScheduleData]]:
        """获取指定群聊内所有已绑定课表的成员列表；若群聊为空或未匹配到，回退检查已绑定用户"""
        bound_in_group: List[Tuple[UserProfile, ScheduleData]] = []
        all_bound: List[Tuple[UserProfile, ScheduleData]] = []

        for uid, prof in self._profiles.items():
            sched = self.get_schedule(uid)
            if not sched:
                continue
            all_bound.append((prof, sched))
            if group_id and str(group_id) in prof.groups:
                bound_in_group.append((prof, sched))

        if group_id:
            return bound_in_group
        return all_bound

    def get_all_bound_users(self) -> List[Tuple[UserProfile, ScheduleData]]:
        result: List[Tuple[UserProfile, ScheduleData]] = []
        for uid, prof in self._profiles.items():
            sched = self.get_schedule(uid)
            if sched:
                result.append((prof, sched))
        return result
