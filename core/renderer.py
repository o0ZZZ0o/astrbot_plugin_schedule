from __future__ import annotations

import os
import shutil
import ssl
import struct
import subprocess
import tempfile
import time
import urllib.request
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    DEFAULT_TIME_SLOTS,
    WEEKDAY_FULL_NAMES,
    WEEKDAY_NAMES,
    CourseInstance,
    ScheduleData,
    UserProfile,
    format_duration_cn,
    hhmm_to_minutes,
    now_in_tz,
    parse_hhmm,
)
from .storage import StorageManager, safe_filename

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from PIL import ImageFilter  # type: ignore
except ImportError:
    ImageFilter = None  # type: ignore


FONT_SEARCH_PATHS = [
    # Debian / Ubuntu / Linux 常见中文字体路径
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # macOS
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

# Debian 官方及国内高速镜像站的文泉驿微米黑字体 deb 包
DEBIAN_FONT_DEB_MIRRORS = [
    "https://mirrors.aliyun.com/debian/pool/main/f/fonts-wqy-microhei/fonts-wqy-microhei_0.2.0-beta-3.1_all.deb",
    "https://mirrors.tuna.tsinghua.edu.cn/debian/pool/main/f/fonts-wqy-microhei/fonts-wqy-microhei_0.2.0-beta-3.1_all.deb",
    "https://mirrors.ustc.edu.cn/debian/pool/main/f/fonts-wqy-microhei/fonts-wqy-microhei_0.2.0-beta-3.1_all.deb",
    "https://deb.debian.org/debian/pool/main/f/fonts-wqy-microhei/fonts-wqy-microhei_0.2.0-beta-3.1_all.deb",
]

# 鲜明、现代的高饱和度课程配色板（主色, 浅色背景, 深色文字）
VIVID_PALETTES: List[Tuple[Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]]] = [
    ((79, 70, 229), (238, 242, 255), (49, 46, 129)),    # 皇家靛蓝 Indigo
    ((13, 148, 136), (240, 253, 250), (19, 78, 74)),    # 翡翠青绿 Teal
    ((234, 88, 12), (255, 247, 237), (124, 45, 18)),    # 活力珊瑚橙 Coral
    ((37, 99, 235), (239, 246, 255), (30, 58, 138)),    # 电光宝蓝 Blue
    ((147, 51, 234), (250, 245, 255), (88, 28, 135)),   # 极光紫罗兰 Purple
    ((225, 29, 72), (255, 241, 242), (136, 19, 55)),    # 绯红玫瑰 Rose
    ((5, 150, 105), (236, 253, 245), (6, 78, 59)),      # 森林翠绿 Emerald
    ((217, 119, 6), (255, 251, 235), (120, 53, 15)),    # 琥珀暖金 Amber
]


def get_vivid_theme(
    course_name: str, index: int = 0
) -> Tuple[Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]]:
    h = sum(ord(c) for c in (course_name or "课")) + index
    return VIVID_PALETTES[h % len(VIVID_PALETTES)]


def create_minimal_png(
    width: int,
    height: int,
    bg_rgb: Tuple[int, int, int] = (244, 246, 250),
    header_rgb: Tuple[int, int, int] = (79, 70, 229),
    header_height: int = 120,
) -> bytes:
    """纯 Python 标准库生成合法的 PNG 图像字节（用于无 Pillow 单元测试环境兜底）"""
    w = max(64, min(width, 900))
    h = max(64, min(height, 1200))
    hh = min(h, header_height)

    header_row = b"\x00" + bytes(header_rgb) * w
    body_row = b"\x00" + bytes(bg_rgb) * w
    raw_data = header_row * hh + body_row * (h - hh)

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw_data, level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def strip_unsupported_chars(text: str) -> str:
    """移除标准中文字体不支持的彩色 Emoji 字符（避免在 Linux Pillow 下渲染为 □ 方框）"""
    if not text:
        return ""
    cleaned: List[str] = []
    for ch in text:
        cp = ord(ch)
        if 0x1F000 <= cp <= 0x1FAFF:
            continue
        if 0x2600 <= cp <= 0x27BF:
            continue
        if cp in (0xFE0F, 0xFE0E, 0x200D):
            continue
        cleaned.append(ch)
    return "".join(cleaned).strip()


class ScheduleRenderer:
    """
    专为手机 QQ 聊天窗口优化的高清自适应课程表渲染引擎：
    - 动态多行换行 + 字号自适应缩放 + 卡片高度弹性伸缩，确保长课程名、长教室名、排行数据 100% 完整显示不截断
    - 周课表完整展示周一至周日 7 天全部课程（包含周六、周日课程），并自动计算单元格最佳字号与行高
    - 4x 超采样抗锯齿圆形头像与徽章
    - 内置 Debian Linux 中文字体校验与自动修复机制
    """

    def __init__(self, storage: StorageManager, plugin_dir: Optional[Path] = None):
        self.storage = storage
        self.plugin_dir = Path(plugin_dir) if plugin_dir else storage.data_dir
        self._font_cache: Dict[Tuple[int, bool], Any] = {}
        self._font_path: Optional[str] = None
        self._bold_index: int = 0
        self._reg_index: int = 0
        self._init_cjk_font()

    def _font_supports_chinese(self, font_path: str, face_index: int = 0) -> bool:
        if not HAS_PIL:
            return True
        try:
            f = ImageFont.truetype(font_path, 28, index=face_index)
            m1 = bytes(f.getmask("课"))
            m2 = bytes(f.getmask("程"))
            m3 = bytes(f.getmask("表"))
            if not m1 or not m2:
                return False
            return (m1 != m2) or (m2 != m3)
        except Exception:
            return False

    def _try_use_font_candidate(self, cand: str) -> bool:
        if not cand or not Path(cand).exists():
            return False
        if self._font_supports_chinese(cand, 0):
            self._font_path = cand
            self._reg_index = 0
            if cand.lower().endswith(".ttc"):
                if self._font_supports_chinese(cand, 2):
                    self._bold_index = 2
                elif self._font_supports_chinese(cand, 1):
                    self._bold_index = 1
                else:
                    self._bold_index = 0
            else:
                self._bold_index = 0
            return True
        return False

    def _init_cjk_font(self) -> None:
        """在 Debian / Linux / macOS / Windows 上按优先级快速定位或自动获取中文字体"""
        for folder in (self.plugin_dir / "fonts", self.storage.data_dir / "fonts"):
            if folder.exists():
                for ext in ("*.ttc", "*.ttf", "*.otf"):
                    for p in sorted(folder.glob(ext)):
                        if self._try_use_font_candidate(str(p)):
                            return

        for p_str in FONT_SEARCH_PATHS:
            if self._try_use_font_candidate(p_str):
                return

        try:
            proc = subprocess.run(
                ["fc-list", ":lang=zh", "file"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if proc.returncode == 0 and proc.stdout:
                for line in proc.stdout.splitlines():
                    fp = line.split(":")[0].strip()
                    if self._try_use_font_candidate(fp):
                        return
        except Exception:
            pass

        for sys_font_root in (
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
        ):
            root_p = Path(sys_font_root)
            if root_p.exists():
                for ext in ("*.ttc", "*.ttf", "*.otf"):
                    for fp in root_p.rglob(ext):
                        if self._try_use_font_candidate(str(fp)):
                            return

        downloaded = self._auto_provision_debian_cjk_font()
        if downloaded:
            self._try_use_font_candidate(downloaded)

    def _auto_provision_debian_cjk_font(self) -> Optional[str]:
        target_dir = self.storage.data_dir / "fonts"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_ttc = target_dir / "wqy-microhei.ttc"
        if target_ttc.exists() and target_ttc.stat().st_size > 100000:
            return str(target_ttc)

        for url in DEBIAN_FONT_DEB_MIRRORS:
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"}, method="GET"
                )
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    deb_bytes = resp.read()
                if len(deb_bytes) > 500000:
                    with tempfile.TemporaryDirectory() as tmp_d:
                        deb_file = Path(tmp_d) / "font.deb"
                        deb_file.write_bytes(deb_bytes)
                        subprocess.run(
                            ["dpkg-deb", "-x", str(deb_file), tmp_d],
                            check=False,
                            timeout=8,
                        )
                        for found in Path(tmp_d).rglob("*.ttc"):
                            shutil.copy2(found, target_ttc)
                            return str(target_ttc)
            except Exception:
                continue
        return None

    def _get_font(self, size: int, bold: bool = False) -> Any:
        if not HAS_PIL:
            return None
        if not self._font_path:
            self._init_cjk_font()
        key = (size, bold)
        if key in self._font_cache:
            return self._font_cache[key]

        font = None
        if self._font_path:
            idx = self._bold_index if bold else self._reg_index
            try:
                font = ImageFont.truetype(self._font_path, size, index=idx)
            except Exception:
                try:
                    font = ImageFont.truetype(self._font_path, size, index=0)
                except Exception:
                    font = None

        if font is None:
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None

        self._font_cache[key] = font
        return font

    # =========================================================================
    # 通用：课程卡片配色（根据上课状态）
    # =========================================================================
    @staticmethod
    def _card_style(c: CourseInstance, idx: int) -> Dict[str, Any]:
        v_main, v_bg, v_dark = get_vivid_theme(c.course_name, idx)
        if c.status == "ongoing":
            return {
                "accent": (16, 185, 129),
                "pill_bg": (16, 185, 129),
                "pill_fg": (255, 255, 255),
                "label": f"进行中 · 剩{format_duration_cn(c.wait_or_remain_minutes)}",
                "card_bg": (255, 255, 255),
                "outline": (110, 231, 183),
                "title": INK,
                "time": (4, 120, 87),
                "tint_bg": (236, 253, 245),
                "tint_fg": (4, 120, 87),
                "meta": INK_2,
                "track": (209, 250, 229),
            }
        if c.status == "next":
            return {
                "accent": (59, 130, 246),
                "pill_bg": (37, 99, 235),
                "pill_fg": (255, 255, 255),
                "label": f"下一节 · {format_duration_cn(c.wait_or_remain_minutes)}后",
                "card_bg": (255, 255, 255),
                "outline": (147, 197, 253),
                "title": INK,
                "time": (30, 64, 175),
                "tint_bg": (239, 246, 255),
                "tint_fg": (29, 78, 216),
                "meta": INK_2,
                "track": (219, 234, 254),
            }
        if c.status == "finished":
            return {
                "accent": (203, 213, 225),
                "pill_bg": (241, 245, 249),
                "pill_fg": (100, 116, 139),
                "label": "已下课",
                "card_bg": (251, 252, 253),
                "outline": None,
                "title": (100, 116, 139),
                "time": (148, 163, 184),
                "tint_bg": (241, 245, 249),
                "tint_fg": (100, 116, 139),
                "meta": (148, 163, 184),
                "track": (241, 245, 249),
            }
        return {
            "accent": v_main,
            "pill_bg": v_bg,
            "pill_fg": v_dark,
            "label": f"{c.start_time} 开课",
            "card_bg": (255, 255, 255),
            "outline": None,
            "title": INK,
            "time": INK,
            "tint_bg": v_bg,
            "tint_fg": v_dark,
            "meta": INK_2,
            "track": v_bg,
        }

    # =========================================================================
    # 通用：单日课程卡片（左侧时间轨 + 右侧课程详情，长文本完整换行）
    # =========================================================================
    def _measure_course_card(self, m: Any, c: CourseInstance, w: int) -> Dict[str, Any]:
        """预计算课程卡片的换行文本与高度，确保课程名/教室/教师/周数 100% 完整显示"""
        if not isinstance(m, _Canvas):
            m = _Canvas.measurer(self, draw=m, scale=1)

        inner_w = w - CARD_RAIL_W - 24
        t_size, t_lh = 30, 40
        course_lines = m.wrap(c.course_name, t_size, inner_w, bold=True)
        if len(course_lines) > 2:
            t_size, t_lh = 27, 36
            course_lines = m.wrap(c.course_name, t_size, inner_w, bold=True)
        if not course_lines:
            course_lines = ["未命名课程"]

        room_clean = strip_unsupported_chars(c.room) or "未指定"
        teacher_clean = strip_unsupported_chars(c.teacher)
        week_clean = strip_unsupported_chars(getattr(c, "week_range_str", ""))
        raw_rows: List[Tuple[str, str]] = [("教室", room_clean)]
        if teacher_clean and teacher_clean not in room_clean:
            raw_rows.append(("教师", teacher_clean))
        if week_clean:
            raw_rows.append(("周数", week_clean))

        val_w = inner_w - META_LABEL_W
        meta_rows: List[Tuple[str, List[str]]] = []
        for lbl, val in raw_rows:
            lines = m.wrap(val, 20, val_w) or [val]
            meta_rows.append((lbl, lines))
        meta_lines = [ln for _, lines in meta_rows for ln in lines]

        right_h = 30 + 14 + len(course_lines) * t_lh + 8 + len(meta_lines) * 30
        if c.status == "ongoing":
            right_h += 22
        height = max(118, right_h) + 48
        return {
            "t_size": t_size,
            "course_lines": course_lines,
            "course_line_h": t_lh,
            "meta_rows": meta_rows,
            "meta_lines": meta_lines,
            "height": height,
        }

    def _draw_course_card(
        self,
        cv: "_Canvas",
        c: CourseInstance,
        idx: int,
        x: float,
        y: float,
        w: float,
        layout_info: Optional[Dict[str, Any]] = None,
    ) -> float:
        if layout_info is None:
            layout_info = self._measure_course_card(cv, c, int(w))
        h = layout_info["height"]
        st = self._card_style(c, idx)

        cv.rect(
            (x, y, x + w, y + h),
            22,
            fill=st["card_bg"],
            outline=st["outline"],
            width=2 if st["outline"] else 0,
        )

        # 左侧时间轨：开始时间（大）/ 结束时间 / 节次胶囊
        rx = x + 26
        cv.text(rx, y + 44, c.start_time, 32, st["time"], bold=True)
        cv.text(rx, y + 80, c.end_time, 21, INK_3)
        cv.pill(rx, y + 116, c.node_range_str, 16, st["tint_fg"], st["tint_bg"], pad=11, h=28)

        # 分隔色条
        cv.rect((x + CARD_RAIL_W - 18, y + 24, x + CARD_RAIL_W - 14, y + h - 24), 2, fill=st["accent"])

        cx = x + CARD_RAIL_W
        right = x + w - 24

        # 状态胶囊
        cv.pill(cx, y + 39, st["label"], 17, st["pill_fg"], st["pill_bg"], pad=12, h=30)

        # 课程名（多行完整显示）
        ty = y + 24 + 30 + 14
        t_size = layout_info["t_size"]
        t_lh = layout_info["course_line_h"]
        for line in layout_info["course_lines"]:
            cv.text(cx, ty + t_lh / 2, line, t_size, st["title"], bold=True)
            ty += t_lh

        # 教室 / 教师 / 周数（标签 + 值，多行完整显示）
        ty += 8
        for lbl, lines in layout_info["meta_rows"]:
            cv.text(cx, ty + 15, lbl, 17, INK_4, bold=True)
            for ln in lines:
                cv.text(cx + META_LABEL_W, ty + 15, ln, 20, st["meta"])
                ty += 30

        # 进行中课程：进度条
        if c.status == "ongoing":
            by = ty + 10
            cv.rect((cx, by, right, by + 8), 4, fill=st["track"])
            ratio = min(1.0, max(0.04, float(getattr(c, "progress", 0.0) or 0.0)))
            cv.rect((cx, by, cx + (right - cx) * ratio, by + 8), 4, fill=st["accent"])
        return h

    # =========================================================================
    # 通用：渐变 Hero 头图 + 数据块
    # =========================================================================
    def _draw_hero_base(
        self,
        cv: "_Canvas",
        box: Tuple[float, float, float, float],
        c1: Tuple[int, int, int],
        c2: Tuple[int, int, int],
        shadow_col: Tuple[int, int, int],
    ) -> None:
        x1, y1, x2, y2 = box
        w = x2 - x1
        h = y2 - y1
        cv.shadow(box, 26, dy=10, blur=18, alpha=0.30, color=shadow_col)
        cv.gradient(
            box,
            26,
            c1,
            c2,
            blobs=[
                (w - 30, -10, 130, 0.10),
                (w - 150, h + 30, 100, 0.07),
                (w * 0.42, -60, 70, 0.05),
            ],
        )

    def _draw_stat_tiles(
        self,
        cv: "_Canvas",
        x: float,
        y: float,
        total_w: float,
        items: List[Tuple[str, str, Optional[Tuple[int, int, int]]]],
        label_col: Tuple[int, int, int],
        value_col: Tuple[int, int, int],
        tile_alpha: float = 0.14,
        tile_h: float = 86,
    ) -> None:
        gap = 12
        n = max(1, len(items))
        tw = (total_w - gap * (n - 1)) / n
        for i, (lbl, val, dot_col) in enumerate(items):
            bx = x + i * (tw + gap)
            cv.overlay((bx, y, bx + tw, y + tile_h), 18, (255, 255, 255), tile_alpha)
            lx = bx + 18
            if dot_col is not None:
                cv.ellipse((lx, y + 21, lx + 10, y + 31), fill=dot_col)
                lx += 18
            cv.text(lx, y + 26, lbl, 17, label_col)
            v_size = cv.fit(val, tw - 36, 30, 18, bold=True)
            v_col = value_col if dot_col is None or value_col != (255, 255, 255) else value_col
            cv.text(bx + 18, y + 60, val, v_size, v_col, bold=True)

    def _draw_footer(self, cv: "_Canvas", text: str) -> None:
        cv.text(cv.w / 2, cv.h - 30, text, 16, INK_4, anchor="mm")

    # =========================================================================
    # 样式一（A）：个人单日课表
    # =========================================================================
    def render_personal_daily(
        self,
        profile: UserProfile,
        schedule: ScheduleData,
        target_date: date,
        now: Optional[datetime] = None,
        group_id: Optional[str] = None,
        avatar_path: Optional[Path] = None,
        title_prefix: str = "今日课表",
        only_upcoming: bool = False,
        tomorrow_preview: Optional[List[CourseInstance]] = None,
        tz_name: str = "Asia/Shanghai",
    ) -> Path:
        if now is None:
            now = now_in_tz(tz_name)

        all_courses = schedule.get_courses_for_date(target_date, now=now, tz_name=tz_name)
        upcoming_courses = [
            c for c in all_courses if c.status in ("ongoing", "next", "upcoming")
        ]
        show_courses = upcoming_courses if (only_upcoming and upcoming_courses) else all_courses
        show_tomorrow = (
            tomorrow_preview
            if (only_upcoming and not upcoming_courses and tomorrow_preview)
            else None
        )

        display_name = profile.get_display_name(group_id)
        week_num = schedule.get_week_number(target_date)
        weekday_str = WEEKDAY_FULL_NAMES[target_date.isoweekday() - 1]
        total_nodes = sum(c.step for c in all_courses)
        total_hours = round(sum(c.duration_minutes for c in all_courses) / 60.0, 1)

        out_path = (
            self.storage.renders_dir
            / f"personal_daily_{safe_filename(profile.user_id)}_{target_date.isoformat()}_{int(time.time() * 1000) % 100000}.png"
        )
        if not HAS_PIL:
            out_path.write_bytes(
                create_minimal_png(720, 920, (242, 244, 248), (79, 70, 229), 200)
            )
            return out_path

        W = 720
        P = 24
        CW = W - P * 2
        HERO_H = 252
        GAP = 16

        m = _Canvas.measurer(self, scale=SS_MOBILE)
        today_layouts = [self._measure_course_card(m, c, CW) for c in show_courses]
        tomorrow_layouts = (
            [self._measure_course_card(m, tc, CW) for tc in show_tomorrow]
            if show_tomorrow
            else []
        )
        show_done_banner = bool(only_upcoming and all_courses and not upcoming_courses)

        content_h = P + HERO_H + 22
        if show_done_banner:
            content_h += 60 + GAP
        if today_layouts:
            content_h += sum(l["height"] + GAP for l in today_layouts)
        else:
            content_h += (230 if show_tomorrow else 300) + GAP
        if tomorrow_layouts:
            content_h += 54 + sum(l["height"] + GAP for l in tomorrow_layouts)
        H = max(560, content_h + 50)

        cv = _Canvas(self, W, H, bg=BG, scale=SS_MOBILE)
        cv.gradient((0, 0, W, 360), 0, (233, 234, 252), BG, vertical=True)

        # ---------------- Hero ----------------
        hx1, hy1, hx2, hy2 = P, P, W - P, P + HERO_H
        self._draw_hero_base(cv, (hx1, hy1, hx2, hy2), INDIGO_1, INDIGO_2, (67, 56, 202))
        cv.avatar(avatar_path, display_name, hx1 + 24, hy1 + 26, 84, ring=(199, 191, 255), ring_w=4)

        tag_x1, _ = cv.pill(hx2 - 24, hy1 + 46, title_prefix, 19, INDIGO_1, (255, 255, 255), pad=16, h=38, align="right")
        name_w = max(140, tag_x1 - (hx1 + 128) - 14)
        n_size = cv.fit(display_name, name_w, 32, 18, bold=True)
        cv.text(hx1 + 128, hy1 + 50, display_name, n_size, (255, 255, 255), bold=True)

        week_label = (
            f"第 {week_num}/{schedule.max_week} 周" if schedule.max_week > 0 else f"第 {week_num} 周"
        )
        date_txt = f"{target_date.strftime('%m月%d日')} {weekday_str} · {week_label}"
        d_size = cv.fit(date_txt, hx2 - 24 - (hx1 + 128), 20, 14)
        cv.text(hx1 + 128, hy1 + 90, date_txt, d_size, (224, 231, 255))

        self._draw_stat_tiles(
            cv,
            hx1 + 24,
            hy1 + 142,
            CW - 48,
            [
                ("明日课程" if "明日" in title_prefix else "今日课程", f"{len(all_courses)} 门", None),
                ("总计节次", f"{total_nodes} 节", None),
                ("课堂时长", f"{total_hours} 小时", None),
            ],
            label_col=(221, 214, 254),
            value_col=(255, 255, 255),
        )

        y = hy2 + 22

        if show_done_banner:
            cv.rect((P, y, W - P, y + 60), 18, fill=(236, 253, 245), outline=(167, 243, 208), width=1.5)
            done_msg = "今日课程已全部下课 · 以下为今日已上课程回顾"
            fs = cv.fit(done_msg, CW - 90, 21, 15, bold=True)
            msg_w = cv.tw(done_msg, fs, bold=True)
            start_x = (W - (msg_w + 34)) / 2
            cv.ellipse((start_x, y + 18, start_x + 24, y + 42), fill=(16, 185, 129))
            cv.line([(start_x + 6.5, y + 30.5), (start_x + 10.5, y + 34.5), (start_x + 18, y + 25.5)], (255, 255, 255), 2.4)
            cv.text(start_x + 34, y + 30, done_msg, fs, (4, 120, 87), bold=True)
            y += 60 + GAP

        if show_courses:
            positions = []
            yy = y
            for l_info in today_layouts:
                positions.append(yy)
                yy += l_info["height"] + GAP
            for py, l_info in zip(positions, today_layouts):
                cv.shadow((P, py, W - P, py + l_info["height"]), 22, dy=6, blur=14, alpha=0.08)
            for idx, (c, py, l_info) in enumerate(zip(show_courses, positions, today_layouts)):
                self._draw_course_card(cv, c, idx, P, py, CW, l_info)
            y = yy
        else:
            box_h = 230 if show_tomorrow else 300
            cv.shadow((P, y, W - P, y + box_h), 24, dy=6, blur=14, alpha=0.07)
            cv.rect((P, y, W - P, y + box_h), 24, fill=(255, 255, 255))
            mid = W / 2
            icon_cy = y + box_h * 0.36
            cv.ellipse((mid - 40, icon_cy - 40, mid + 40, icon_cy + 40), fill=(238, 242, 255))
            cv.ellipse((mid - 20, icon_cy - 22, mid + 18, icon_cy + 16), fill=INDIGO_1)
            cv.ellipse((mid - 10, icon_cy - 30, mid + 26, icon_cy + 6), fill=(238, 242, 255))
            cv.text(mid, icon_cy + 70, "当天无任何课程安排", 30, INK, bold=True, anchor="mm")
            cv.text(mid, icon_cy + 110, "全天自由支配时间 · 尽情休息或约群友吧", 20, INK_3, anchor="mm")
            y += box_h + GAP

        if show_tomorrow and tomorrow_layouts:
            cv.rect((P + 4, y + 14, P + 10, y + 38), 3, fill=INDIGO_1)
            cv.text(P + 22, y + 26, f"明日课程预览（共 {len(show_tomorrow)} 门课）", 23, INK, bold=True)
            y += 54
            positions = []
            yy = y
            for l_info in tomorrow_layouts:
                positions.append(yy)
                yy += l_info["height"] + GAP
            for py, l_info in zip(positions, tomorrow_layouts):
                cv.shadow((P, py, W - P, py + l_info["height"]), 22, dy=6, blur=14, alpha=0.08)
            for idx, (tc, py, l_info) in enumerate(zip(show_tomorrow, positions, tomorrow_layouts)):
                self._draw_course_card(cv, tc, idx + 3, P, py, CW, l_info)
            y = yy

        self._draw_footer(cv, f"AstrBot 课程表  ·  {now.strftime('%Y-%m-%d %H:%M')}")
        cv.save(out_path)
        return out_path

    # =========================================================================
    # 样式一（B）：个人周课表网格（周一至周日 7 列 + 动态节次时间轴）
    # =========================================================================
    def _fit_weekly_cell_content(
        self,
        m: "_Canvas",
        c: CourseInstance,
        inner_w: float,
        avail_h: float,
        custom_time_str: str = "",
    ) -> Dict[str, Any]:
        """为周课表色块自动选择最佳字号并完整换行（不截断课程名、教室与教师）"""
        room_clean = strip_unsupported_chars(c.room)
        teacher_clean = strip_unsupported_chars(c.teacher)
        if room_clean and teacher_clean and teacher_clean not in room_clean:
            meta_str = f"@{room_clean} {teacher_clean}"
        elif room_clean:
            meta_str = f"@{room_clean}"
        elif teacher_clean:
            meta_str = teacher_clean
        else:
            meta_str = ""
        if custom_time_str:
            meta_str = f"{custom_time_str} {meta_str}".strip()

        size_candidates = [
            (20, 16, 26, 21),
            (19, 15, 24, 20),
            (18, 14, 23, 19),
            (16, 13, 20, 17),
            (15, 12, 19, 16),
            (14, 12, 17, 15),
            (12, 11, 15, 14),
        ]
        best: Optional[Dict[str, Any]] = None
        for name_sz, meta_sz, name_lh, meta_lh in size_candidates:
            name_lines = m.wrap(c.course_name, name_sz, inner_w, bold=True)
            meta_lines = m.wrap(meta_str, meta_sz, inner_w) if meta_str else []
            needed_h = len(name_lines) * name_lh + ((5 + len(meta_lines) * meta_lh) if meta_lines else 0)
            best = {
                "name_sz": name_sz,
                "meta_sz": meta_sz,
                "name_lines": name_lines,
                "meta_lines": meta_lines,
                "name_lh": name_lh,
                "meta_lh": meta_lh,
                "needed_h": needed_h,
            }
            if needed_h <= avail_h:
                return best
        assert best is not None
        return best

    def render_personal_weekly(
        self,
        profile: UserProfile,
        schedule: ScheduleData,
        monday_date: date,
        now: Optional[datetime] = None,
        group_id: Optional[str] = None,
        avatar_path: Optional[Path] = None,
        title_prefix: str = "本周课表",
        tz_name: str = "Asia/Shanghai",
    ) -> Path:
        if now is None:
            now = now_in_tz(tz_name)
        monday = monday_date - timedelta(days=monday_date.isoweekday() - 1)
        sunday = monday + timedelta(days=6)
        week_num = schedule.get_week_number(monday)
        week_courses = schedule.get_courses_for_week(monday, now=now, tz_name=tz_name)
        display_name = profile.get_display_name(group_id)

        out_path = (
            self.storage.renders_dir
            / f"personal_weekly_{safe_filename(profile.user_id)}_{monday.isoformat()}_{int(time.time() * 1000) % 100000}.png"
        )
        if not HAS_PIL:
            out_path.write_bytes(
                create_minimal_png(1060, 1180, (244, 246, 250), (49, 46, 129), 160)
            )
            return out_path

        max_course_node = max(
            (c.end_node for dlist in week_courses.values() for c in dlist), default=0
        )
        all_sched_node = max(
            (c.start_node + max(1, c.step) - 1 for c in schedule.courses), default=0
        )
        max_nodes = max(4, schedule.max_nodes, max_course_node, all_sched_node)
        slot_map = {s.node: s for s in schedule.time_slots}
        default_map = {s["node"]: s for s in DEFAULT_TIME_SLOTS}

        def _get_custom_time_tag(c: CourseInstance) -> str:
            s_node = max(1, min(max_nodes, c.start_node))
            e_node = max(s_node, min(max_nodes, c.end_node))
            s_slot = slot_map.get(s_node)
            e_slot = slot_map.get(e_node)
            if not s_slot or not e_slot:
                return f"{c.start_time}-{c.end_time}"
            st_diff = abs(hhmm_to_minutes(c.start_time) - hhmm_to_minutes(s_slot.start_time))
            et_diff = abs(hhmm_to_minutes(c.end_time) - hhmm_to_minutes(e_slot.end_time))
            if st_diff > 1 or et_diff > 1:
                return f"{c.start_time}-{c.end_time}"
            return ""

        num_days = 7
        W = 1080
        P = 24
        HERO_H = 150
        COL_H = 76
        AXIS_W = 92
        grid_w = W - P * 2 - AXIS_W
        col_w = grid_w / num_days
        BLOCK_INSET = 3
        TEXT_PAD_L = 13
        TEXT_PAD_R = 7
        inner_cell_w = col_w - BLOCK_INSET * 2 - TEXT_PAD_L - TEXT_PAD_R

        m = _Canvas.measurer(self, scale=SS_WEEKLY)
        row_h = 96
        for day_list in week_courses.values():
            for c in day_list:
                span = max(1, c.end_node - c.start_node + 1)
                avail_h = span * row_h - BLOCK_INSET * 2 - 20
                plan = self._fit_weekly_cell_content(m, c, inner_cell_w, avail_h, _get_custom_time_tag(c))
                if plan["needed_h"] > avail_h:
                    req = int((plan["needed_h"] + BLOCK_INSET * 2 + 22) / span) + 2
                    if req > row_h:
                        row_h = min(140, req)

        grid_top = P + HERO_H + 18
        body_top = grid_top + COL_H
        grid_bottom = body_top + max_nodes * row_h
        H = grid_bottom + 56

        cv = _Canvas(self, W, H, bg=BG, scale=SS_WEEKLY)
        cv.gradient((0, 0, W, 420), 0, (233, 234, 252), BG, vertical=True)

        # ---------------- Hero ----------------
        hx1, hy1, hx2, hy2 = P, P, W - P, P + HERO_H
        self._draw_hero_base(cv, (hx1, hy1, hx2, hy2), INDIGO_1, INDIGO_2, (67, 56, 202))
        cv.avatar(avatar_path, display_name, hx1 + 26, hy1 + 27, 92, ring=(199, 191, 255), ring_w=4)

        all_wk = [c for dlist in week_courses.values() for c in dlist]
        total_wk_nodes = sum(c.step for c in all_wk)
        total_wk_hours = round(sum(c.duration_minutes for c in all_wk) / 60.0, 1)
        week_badge = (
            f"第 {week_num}/{schedule.max_week} 周" if schedule.max_week > 0 else f"第 {week_num} 周"
        )
        head_raw = f"{display_name} · {title_prefix}（{week_badge}）"
        text_x = hx1 + 146
        h_size = cv.fit(head_raw, hx2 - 28 - text_x, 32, 20, bold=True)
        cv.text(text_x, hy1 + 56, head_raw, h_size, (255, 255, 255), bold=True)
        sub_str = (
            f"{monday.strftime('%m/%d')} - {sunday.strftime('%m/%d')}（周一至周日）  ·  共 {len(all_wk)} 门次 / {total_wk_nodes} 节 / {total_wk_hours} 小时"
        )
        s_size = cv.fit(sub_str, hx2 - 28 - text_x, 20, 14)
        cv.text(text_x, hy1 + 100, sub_str, s_size, (224, 231, 255))

        # ---------------- 网格外框 ----------------
        gx1, gx2 = P, W - P
        cv.shadow((gx1, grid_top, gx2, grid_bottom), 24, dy=8, blur=16, alpha=0.08)
        cv.rect((gx1, grid_top, gx2, grid_bottom), 24, fill=(255, 255, 255))

        today = now.date()
        today_idx = (today - monday).days if monday <= today <= sunday else -1

        # 今日列柔和高亮（圆角整列）
        if 0 <= today_idx < num_days:
            tx1 = gx1 + AXIS_W + today_idx * col_w
            cv.rect((tx1 + 3, grid_top + 6, tx1 + col_w - 3, grid_bottom - 6), 16, fill=(244, 243, 255))

        # 节次表头
        cv.text(gx1 + AXIS_W / 2, grid_top + COL_H / 2, "节次", 20, INK_3, bold=True, anchor="mm")

        for d_idx in range(num_days):
            cur_d = monday + timedelta(days=d_idx)
            cx1 = gx1 + AXIS_W + d_idx * col_w
            mid = cx1 + col_w / 2
            is_today = d_idx == today_idx
            if is_today:
                cv.gradient((cx1 + 8, grid_top + 10, cx1 + col_w - 8, grid_top + COL_H - 10), 14, INDIGO_1, INDIGO_2)
                fg1, fg2 = (255, 255, 255), (224, 231, 255)
            else:
                is_weekend = d_idx >= 5
                fg1 = (INK if not is_weekend else (71, 85, 105))
                fg2 = INK_4
            cv.text(mid, grid_top + 30, WEEKDAY_NAMES[d_idx], 21, fg1, bold=True, anchor="mm")
            cv.text(mid, grid_top + 54, cur_d.strftime("%m/%d"), 15, fg2, anchor="mm")

        # 表头分割线
        cv.line([(gx1 + 16, body_top), (gx2 - 16, body_top)], LINE, 1.2)

        # 节次时间轴 + 行分割线（大课间/午休/晚休 >= 25 分钟使用靛蓝虚线）
        for node in range(1, max_nodes + 1):
            ry1 = body_top + (node - 1) * row_h
            slot = slot_map.get(node)
            prev_slot = slot_map.get(node - 1) if node > 1 else None
            st_str = slot.start_time if slot else default_map.get(node, {}).get("start_time", "")
            et_str = slot.end_time if slot else default_map.get(node, {}).get("end_time", "")

            if node > 1:
                is_big_break = False
                if prev_slot and st_str and prev_slot.end_time:
                    gap_mins = hhmm_to_minutes(st_str) - hhmm_to_minutes(prev_slot.end_time)
                    if gap_mins >= 25:
                        is_big_break = True
                if is_big_break:
                    cv.dashed_hline(gx1 + 12, gx2 - 12, ry1, (165, 180, 252), 2, dash=9, gap=6)
                else:
                    cv.line([(gx1 + 16, ry1), (gx2 - 16, ry1)], LINE_SOFT, 1)

            ax = gx1 + AXIS_W / 2
            cv.text(ax, ry1 + 26, f"{node}", 22, INK, bold=True, anchor="mm")
            if st_str:
                cv.text(ax, ry1 + 52, st_str, 14, INK_3, anchor="mm")
            if et_str:
                cv.text(ax, ry1 + 70, et_str, 14, INK_4, anchor="mm")

        # 竖向分割线
        for d_idx in range(1, num_days):
            cx = gx1 + AXIS_W + d_idx * col_w
            cv.line([(cx, grid_top + 18), (cx, grid_bottom - 18)], LINE_SOFT, 1)
        cv.line([(gx1 + AXIS_W, grid_top + 18), (gx1 + AXIS_W, grid_bottom - 18)], LINE, 1.2)

        # 课程色块
        for wd, day_courses in week_courses.items():
            d_idx = wd - 1
            if not (0 <= d_idx < num_days):
                continue
            bx1 = gx1 + AXIS_W + d_idx * col_w + BLOCK_INSET
            bx2 = bx1 + col_w - BLOCK_INSET * 2
            for idx, c in enumerate(day_courses):
                s_node = max(1, min(max_nodes, c.start_node))
                e_node = max(s_node, min(max_nodes, c.end_node))
                by1 = body_top + (s_node - 1) * row_h + BLOCK_INSET
                by2 = body_top + e_node * row_h - BLOCK_INSET

                v_main, v_bg, v_dark = get_vivid_theme(c.course_name, idx)
                cv.rect((bx1, by1, bx2, by2), 12, fill=v_bg, outline=_mix(v_bg, v_main, 0.28), width=1)
                cv.rect((bx1, by1, bx1 + 5, by2), 2.5, fill=v_main)

                plan = self._fit_weekly_cell_content(
                    cv,
                    c,
                    (bx2 - bx1) - TEXT_PAD_L - TEXT_PAD_R,
                    (by2 - by1) - 20,
                    _get_custom_time_tag(c),
                )
                tx = bx1 + TEXT_PAD_L
                ty = by1 + 10
                for ln in plan["name_lines"]:
                    cv.text(tx, ty + plan["name_lh"] / 2, ln, plan["name_sz"], v_dark, bold=True)
                    ty += plan["name_lh"]
                if plan["meta_lines"]:
                    ty += 5
                    meta_col = _mix(v_main, v_dark, 0.35)
                    for ln in plan["meta_lines"]:
                        cv.text(tx, ty + plan["meta_lh"] / 2, ln, plan["meta_sz"], meta_col)
                        ty += plan["meta_lh"]

        self._draw_footer(cv, f"AstrBot 课程表  ·  第 {week_num} 周  ·  {now.strftime('%Y-%m-%d %H:%M')}")
        cv.save(out_path)
        return out_path

    # =========================================================================
    # 样式二：群友课表总览（实时状态 / 明日课程）
    # =========================================================================
    def render_group_overview(
        self,
        members_data: List[Tuple[UserProfile, ScheduleData, Optional[Path]]],
        group_id: Optional[str] = None,
        now: Optional[datetime] = None,
        is_tomorrow: bool = False,
        tz_name: str = "Asia/Shanghai",
    ) -> Path:
        if now is None:
            now = now_in_tz(tz_name)
        target_date = (now.date() + timedelta(days=1)) if is_tomorrow else now.date()
        weekday_str = WEEKDAY_FULL_NAMES[target_date.isoweekday() - 1]

        out_path = (
            self.storage.renders_dir
            / f"group_{'tomorrow' if is_tomorrow else 'now'}_{safe_filename(group_id or 'all')}_{int(time.time() * 1000) % 100000}.png"
        )
        if not HAS_PIL:
            out_path.write_bytes(
                create_minimal_png(720, 920, (240, 245, 245), (15, 118, 110), 210)
            )
            return out_path

        W = 720
        P = 24
        CW = W - P * 2
        HERO_H = 244
        GAP = 16
        AV = 80
        CONTENT_X = P + 24 + AV + 24
        BOX_X2 = W - P - 22
        MAIN_SZ, MAIN_LH = 23, 34
        SUB_SZ, SUB_LH = 19, 28
        text_max_w = (BOX_X2 - CONTENT_X) - 32

        m = _Canvas.measurer(self, scale=SS_MOBILE)
        rows: List[Dict[str, Any]] = []
        ongoing_count = 0
        free_count = 0
        has_class_tomorrow_count = 0

        for prof, sched, av_path in members_data:
            day_courses = sched.get_courses_for_date(target_date, now=now, tz_name=tz_name)
            m_week = sched.get_week_number(target_date)
            ongoing_c = next((c for c in day_courses if c.status == "ongoing"), None)
            next_c = next((c for c in day_courses if c.status in ("next", "upcoming")), None)
            thresh_str = getattr(prof, "early_eight_threshold", "08:00") or "08:00"
            t_thresh = parse_hhmm(thresh_str)

            if is_tomorrow:
                if day_courses:
                    has_class_tomorrow_count += 1
                    sort_rank = (0, day_courses[0].start_time, -len(day_courses))
                    accent, box_bg, box_fg = (37, 99, 235), (239, 246, 255), (30, 58, 138)
                    total_nodes = sum(c.step for c in day_courses)
                    early_str = " · 早八" if parse_hhmm(day_courses[0].start_time) <= t_thresh else ""
                    pill_txt = f"明日 {len(day_courses)} 门 ({total_nodes}节){early_str}"
                    main_lines: List[str] = []
                    for c in day_courses:
                        loc = f" @{c.room}" if c.room else ""
                        main_lines.extend(
                            m.wrap(f"{c.start_time}-{c.end_time} {c.course_name}{loc}", MAIN_SZ, text_max_w, bold=True)
                        )
                    sub_lines = m.wrap(
                        f"第{m_week}周 · 全天共 {total_nodes} 节 · {format_duration_cn(sum(c.duration_minutes for c in day_courses))}",
                        SUB_SZ,
                        text_max_w,
                    )
                else:
                    free_count += 1
                    sort_rank = (1, "99:99", 0)
                    accent, box_bg, box_fg = (16, 185, 129), (236, 253, 245), (6, 95, 70)
                    pill_txt = "明天全天无课"
                    main_lines = m.wrap("明日无任何课程安排", MAIN_SZ, text_max_w, bold=True)
                    sub_lines = m.wrap(f"第{m_week}周 · 全天自由活动 · 可放心约饭或游戏", SUB_SZ, text_max_w)
            else:
                if ongoing_c:
                    ongoing_count += 1
                    sort_rank = (0, ongoing_c.end_time, 0)
                    accent, box_bg, box_fg = (234, 88, 12), (255, 247, 237), (154, 52, 18)
                    pill_txt = f"上课中 · 剩{format_duration_cn(ongoing_c.wait_or_remain_minutes)}"
                    loc = f" @{ongoing_c.room}" if ongoing_c.room else ""
                    main_raw = f"正在上：{ongoing_c.course_name}{loc}"
                    if next_c:
                        sub_raw = f"{ongoing_c.start_time}-{ongoing_c.end_time} | 下一节 {next_c.start_time} {next_c.course_name}"
                    else:
                        sub_raw = f"{ongoing_c.start_time}-{ongoing_c.end_time}（上完这节今日就下课啦）"
                elif next_c:
                    sort_rank = (1, next_c.start_time, 0)
                    accent, box_bg, box_fg = (37, 99, 235), (239, 246, 255), (30, 64, 175)
                    pill_txt = f"下一节 · {format_duration_cn(next_c.wait_or_remain_minutes)}后"
                    loc = f" @{next_c.room}" if next_c.room else ""
                    main_raw = f"下一节：{next_c.course_name}{loc}"
                    sub_raw = f"上课时间 {next_c.start_time} - {next_c.end_time} ({next_c.node_range_str})"
                else:
                    free_count += 1
                    sort_rank = (2, "99:99", 0)
                    accent, box_bg, box_fg = (16, 185, 129), (236, 253, 245), (6, 95, 70)
                    pill_txt = "空闲 · 随时可约"
                    if day_courses:
                        main_raw = f"今日 {len(day_courses)} 门课已全部上完"
                        sub_raw = f"最后下课时间 {day_courses[-1].end_time} · 当前完全空闲"
                    else:
                        main_raw = "今日全天无课"
                        sub_raw = "当前完全空闲 · 随时可以约饭或开黑"
                main_lines = m.wrap(main_raw, MAIN_SZ, text_max_w, bold=True)
                sub_lines = m.wrap(sub_raw, SUB_SZ, text_max_w)

            inner_box_h = 16 + len(main_lines) * MAIN_LH + 4 + len(sub_lines) * SUB_LH + 14
            card_h = max(AV + 48, 76 + inner_box_h + 22)
            rows.append(
                {
                    "display_name": prof.get_display_name(group_id),
                    "avatar_path": av_path,
                    "accent": accent,
                    "box_bg": box_bg,
                    "box_fg": box_fg,
                    "pill_txt": pill_txt,
                    "main_lines": main_lines,
                    "sub_lines": sub_lines,
                    "inner_box_h": inner_box_h,
                    "card_h": card_h,
                    "sort_rank": sort_rank,
                }
            )

        rows.sort(key=lambda r: r["sort_rank"])

        content_h = P + HERO_H + 22 + sum(r["card_h"] + GAP for r in rows)
        if not rows:
            content_h += 200
        H = max(560, content_h + 50)

        cv = _Canvas(self, W, H, bg=BG_TEAL, scale=SS_MOBILE)
        cv.gradient((0, 0, W, 360), 0, (220, 245, 242), BG_TEAL, vertical=True)

        # ---------------- Hero ----------------
        hx1, hy1, hx2, hy2 = P, P, W - P, P + HERO_H
        self._draw_hero_base(cv, (hx1, hy1, hx2, hy2), TEAL_1, TEAL_2, (13, 148, 136))
        title_str = "群友明日课表总览" if is_tomorrow else "群友实时上课状态"
        cv.text(hx1 + 28, hy1 + 50, title_str, 34, (255, 255, 255), bold=True)
        sub_str = f"{target_date.strftime('%Y年%m月%d日')} {weekday_str}  ·  查询时间 {now.strftime('%H:%M')}"
        cv.text(hx1 + 28, hy1 + 96, sub_str, cv.fit(sub_str, CW - 56, 20, 14), (204, 251, 241))

        if is_tomorrow:
            stats = [
                ("已绑群友", f"{len(rows)} 人", (255, 255, 255)),
                ("明日有课", f"{has_class_tomorrow_count} 人", (147, 197, 253)),
                ("明日无课", f"{free_count} 人", (134, 239, 172)),
            ]
        else:
            stats = [
                ("已绑群友", f"{len(rows)} 人", (255, 255, 255)),
                ("正在上课", f"{ongoing_count} 人", (253, 186, 116)),
                ("空闲可约", f"{free_count} 人", (134, 239, 172)),
            ]
        self._draw_stat_tiles(
            cv, hx1 + 24, hy1 + 136, CW - 48, stats,
            label_col=(204, 251, 241), value_col=(255, 255, 255), tile_alpha=0.16,
        )

        y = hy2 + 22
        positions = []
        yy = y
        for r in rows:
            positions.append(yy)
            yy += r["card_h"] + GAP
        for py, r in zip(positions, rows):
            cv.shadow((P, py, W - P, py + r["card_h"]), 22, dy=6, blur=14, alpha=0.08, color=(15, 60, 60))

        for py, r in zip(positions, rows):
            accent = r["accent"]
            cv.rect((P, py, W - P, py + r["card_h"]), 22, fill=(255, 255, 255))
            cv.avatar(r["avatar_path"], r["display_name"], P + 24, py + 24, AV, ring=_mix(accent, (255, 255, 255), 0.55), ring_w=4, dot=accent)

            px1, _ = cv.pill(BOX_X2, py + 44, r["pill_txt"], cv.fit(r["pill_txt"], 250, 18, 14, bold=True), (255, 255, 255), accent, pad=14, h=34, align="right")
            name_w = max(120, px1 - CONTENT_X - 14)
            cv.text(CONTENT_X, py + 44, r["display_name"], cv.fit(r["display_name"], name_w, 28, 17, bold=True), INK, bold=True)

            ib_y1 = py + 76
            ib_y2 = ib_y1 + r["inner_box_h"]
            cv.rect((CONTENT_X, ib_y1, BOX_X2, ib_y2), 16, fill=r["box_bg"])
            cv.rect((CONTENT_X, ib_y1 + 14, CONTENT_X + 4, ib_y2 - 14), 2, fill=_mix(accent, r["box_bg"], 0.3))
            ty = ib_y1 + 16
            for ln in r["main_lines"]:
                cv.text(CONTENT_X + 18, ty + MAIN_LH / 2, ln, MAIN_SZ, r["box_fg"], bold=True)
                ty += MAIN_LH
            ty += 4
            for ln in r["sub_lines"]:
                cv.text(CONTENT_X + 18, ty + SUB_LH / 2, ln, SUB_SZ, INK_2)
                ty += SUB_LH

        if not rows:
            cv.rect((P, y, W - P, y + 180), 22, fill=(255, 255, 255))
            cv.text(W / 2, y + 90, "暂无已绑定课表的群友", 24, INK_3, bold=True, anchor="mm")

        self._draw_footer(cv, f"AstrBot 课程表  ·  {now.strftime('%Y-%m-%d %H:%M')}")
        cv.save(out_path)
        return out_path

    # =========================================================================
    # 样式三：本周/下周上课排行榜（黑金荣耀榜）
    # =========================================================================
    def render_weekly_ranking(
        self,
        members_data: List[Tuple[UserProfile, ScheduleData, Optional[Path]]],
        group_id: Optional[str] = None,
        now: Optional[datetime] = None,
        tz_name: str = "Asia/Shanghai",
        target_monday: Optional[date] = None,
        title_text: str = "本周群友上课排行榜",
    ) -> Path:
        if now is None:
            now = now_in_tz(tz_name)
        if target_monday is not None:
            monday = target_monday - timedelta(days=target_monday.isoweekday() - 1)
        else:
            today = now.date()
            monday = today - timedelta(days=today.isoweekday() - 1)
        sunday = monday + timedelta(days=6)

        out_path = (
            self.storage.renders_dir
            / f"weekly_ranking_{safe_filename(group_id or 'all')}_{monday.isoformat()}_{int(time.time() * 1000) % 100000}.png"
        )
        if not HAS_PIL:
            out_path.write_bytes(
                create_minimal_png(720, 920, (246, 244, 240), (217, 119, 6), 210)
            )
            return out_path

        ranking_rows: List[Dict[str, Any]] = []
        for prof, sched, av_path in members_data:
            wk_map = sched.get_courses_for_week(monday, now=now, tz_name=tz_name)
            wk_courses = [c for dlist in wk_map.values() for c in dlist]
            m_week = sched.get_week_number(monday)
            total_mins = sum(c.duration_minutes for c in wk_courses)
            total_nodes = sum(c.step for c in wk_courses)
            threshold_str = getattr(prof, "early_eight_threshold", "08:00") or "08:00"
            t_thresh = parse_hhmm(threshold_str)
            early_count = sum(1 for c in wk_courses if parse_hhmm(c.start_time) <= t_thresh)
            ranking_rows.append(
                {
                    "display_name": prof.get_display_name(group_id),
                    "avatar_path": av_path,
                    "week_num": m_week,
                    "total_mins": total_mins,
                    "total_hours": round(total_mins / 60.0, 1),
                    "total_nodes": total_nodes,
                    "course_count": len(wk_courses),
                    "early_count": early_count,
                }
            )
        ranking_rows.sort(
            key=lambda x: (x["total_mins"], x["total_nodes"], x["course_count"]), reverse=True
        )
        max_mins = max((r["total_mins"] for r in ranking_rows), default=1) or 1

        W = 720
        P = 24
        CW = W - P * 2
        HERO_H = 244
        GAP = 16
        AV = 84
        CONTENT_X = P + 24 + AV + 26
        RIGHT_X = W - P - 24
        content_w = RIGHT_X - CONTENT_X

        m = _Canvas.measurer(self, scale=SS_MOBILE)
        for r in ranking_rows:
            stats_str = f"第{r['week_num']}周 · 共 {r['total_nodes']} 节 · {r['course_count']} 门课 · 早八 {r['early_count']} 次"
            st_size = m.fit(stats_str, content_w, 19, 15)
            r["stats_size"] = st_size
            r["stats_lines"] = m.wrap(stats_str, st_size, content_w)
            r["card_h"] = 156 + max(1, len(r["stats_lines"])) * 28

        content_h = P + HERO_H + 22 + sum(r["card_h"] + GAP for r in ranking_rows)
        if not ranking_rows:
            content_h += 200
        H = max(560, content_h + 50)

        cv = _Canvas(self, W, H, bg=BG_WARM, scale=SS_MOBILE)
        cv.gradient((0, 0, W, 360), 0, (241, 236, 226), BG_WARM, vertical=True)

        # ---------------- Hero ----------------
        hx1, hy1, hx2, hy2 = P, P, W - P, P + HERO_H
        self._draw_hero_base(cv, (hx1, hy1, hx2, hy2), DARK_1, DARK_2, (40, 30, 10))
        cv.rect((hx1 + 28, hy1, hx1 + 150, hy1 + 6), 3, fill=GOLD)
        cv.text(hx1 + 28, hy1 + 50, title_text, cv.fit(title_text, CW - 56, 34, 22, bold=True), (255, 255, 255), bold=True)
        sub_str = f"统计周期：{monday.strftime('%m月%d日')} - {sunday.strftime('%m月%d日')}（周一至周日）"
        cv.text(hx1 + 28, hy1 + 96, sub_str, cv.fit(sub_str, CW - 56, 20, 14), (253, 230, 138))

        group_total_hours = round(sum(r["total_mins"] for r in ranking_rows) / 60.0, 1)
        group_total_nodes = sum(r["total_nodes"] for r in ranking_rows)
        self._draw_stat_tiles(
            cv, hx1 + 24, hy1 + 136, CW - 48,
            [
                ("上榜人数", f"{len(ranking_rows)} 人", None),
                ("全群总课时", f"{group_total_hours} 小时", None),
                ("全群总节数", f"{group_total_nodes} 节", None),
            ],
            label_col=(212, 212, 216), value_col=(252, 211, 77), tile_alpha=0.08,
        )

        y = hy2 + 22
        positions = []
        yy = y
        for r in ranking_rows:
            positions.append(yy)
            yy += r["card_h"] + GAP
        for idx, (py, r) in enumerate(zip(positions, ranking_rows)):
            sh_col = (180, 120, 0) if idx == 0 else (40, 30, 20)
            cv.shadow((P, py, W - P, py + r["card_h"]), 22, dy=6, blur=14, alpha=0.12 if idx == 0 else 0.07, color=sh_col)

        for idx, (py, r) in enumerate(zip(positions, ranking_rows)):
            rank = idx + 1
            if rank == 1:
                medal = (245, 158, 11)
                ring = (252, 211, 77)
                card_bg = (255, 251, 235)
                outline = (253, 230, 138)
                tag_bg, tag_fg = (254, 243, 199), (180, 83, 9)
                bar = ((251, 191, 36), (245, 158, 11))
                title_tag = "卷王之王" if r["total_mins"] > 0 else "清闲仙人"
            elif rank == 2:
                medal = (100, 116, 139)
                ring = (203, 213, 225)
                card_bg = (255, 255, 255)
                outline = (226, 232, 240)
                tag_bg, tag_fg = (241, 245, 249), (51, 65, 85)
                bar = ((96, 165, 250), (37, 99, 235))
                title_tag = "早八战神" if r["early_count"] >= 2 else "勤学标兵"
            elif rank == 3:
                medal = (217, 119, 6)
                ring = (253, 186, 116)
                card_bg = (255, 255, 255)
                outline = (254, 215, 170)
                tag_bg, tag_fg = (255, 237, 213), (154, 52, 18)
                bar = ((45, 212, 191), (13, 148, 136))
                title_tag = "勤学标兵" if r["total_mins"] >= 450 else "劳逸结合"
            else:
                medal = (148, 163, 184)
                ring = (226, 232, 240)
                card_bg = (255, 255, 255)
                outline = None
                tag_bg, tag_fg = (241, 245, 249), (100, 116, 139)
                bar = ((203, 213, 225), (148, 163, 184))
                title_tag = "清闲仙人" if r["total_mins"] == 0 else "摸鱼行家"
            rank_str = f"NO.{rank}"

            ch = r["card_h"]
            cv.rect((P, py, W - P, py + ch), 22, fill=card_bg, outline=outline, width=1.5 if outline else 0)

            av_x = P + 24
            av_y = py + 24
            cv.avatar(r["avatar_path"], r["display_name"], av_x, av_y, AV, ring=ring, ring_w=4)
            cv.pill(av_x + AV / 2, av_y + AV + 8, rank_str, 16, (255, 255, 255), medal, pad=12, h=28, align="center", outline=(255, 255, 255))

            tag_x1, _ = cv.pill(RIGHT_X, py + 42, title_tag, 17, tag_fg, tag_bg, pad=13, h=32, align="right")
            name_w = max(120, tag_x1 - CONTENT_X - 12)
            cv.text(CONTENT_X, py + 42, r["display_name"], cv.fit(r["display_name"], name_w, 28, 17, bold=True), INK, bold=True)

            hours_val = f"{r['total_hours']}"
            hv_col = (217, 119, 6) if rank == 1 else INK
            cv.text(CONTENT_X, py + 88, hours_val, 36, hv_col, bold=True)
            hw = cv.tw(hours_val, 36, bold=True)
            cv.text(CONTENT_X + hw + 6, py + 94, "小时", 19, INK_3, bold=True)

            ty = py + 118
            for ln in r["stats_lines"]:
                cv.text(CONTENT_X, ty + 14, ln, r["stats_size"], INK_3)
                ty += 28

            bar_y = ty + 10
            cv.rect((CONTENT_X, bar_y, RIGHT_X, bar_y + 12), 6, fill=(241, 241, 244))
            ratio = min(1.0, max(0.06, r["total_mins"] / max_mins)) if r["total_mins"] > 0 else 0.04
            fill_w = max(14, (RIGHT_X - CONTENT_X) * ratio)
            cv.gradient((CONTENT_X, bar_y, CONTENT_X + fill_w, bar_y + 12), 6, bar[0], bar[1])

        if not ranking_rows:
            cv.rect((P, y, W - P, y + 180), 22, fill=(255, 255, 255))
            cv.text(W / 2, y + 90, "暂无已绑定课表的群友", 24, INK_3, bold=True, anchor="mm")

        self._draw_footer(cv, f"AstrBot 课程表  ·  {now.strftime('%Y-%m-%d %H:%M')}")
        cv.save(out_path)
        return out_path


# =============================================================================
# 设计系统常量
# =============================================================================
SS_MOBILE = 3  # 手机竖版图片内部超采样倍率
SS_WEEKLY = 2  # 周课表内部超采样倍率
OUT_SCALE = 1.5  # 最终输出倍率（逻辑 720px → 1080px 高清输出）

CARD_RAIL_W = 152
META_LABEL_W = 52

BG = (244, 245, 250)
BG_TEAL = (240, 247, 246)
BG_WARM = (247, 245, 241)
INK = (15, 23, 42)
INK_2 = (51, 65, 85)
INK_3 = (100, 116, 139)
INK_4 = (148, 163, 184)
LINE = (226, 232, 240)
LINE_SOFT = (238, 241, 246)

INDIGO_1 = (79, 70, 229)
INDIGO_2 = (139, 92, 246)
TEAL_1 = (13, 148, 136)
TEAL_2 = (6, 182, 212)
DARK_1 = (24, 24, 27)
DARK_2 = (68, 52, 24)
GOLD = (245, 158, 11)


def _mix(c1: Tuple[int, int, int], c2: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))  # type: ignore


def _lanczos() -> Any:
    rs = getattr(Image, "Resampling", None)
    if rs is not None and hasattr(rs, "LANCZOS"):
        return rs.LANCZOS
    return getattr(Image, "LANCZOS", 1)


class _Canvas:
    """
    逻辑坐标画布：所有布局使用逻辑像素书写，内部以 scale 倍超采样绘制，
    最终 LANCZOS 缩放输出为 OUT_SCALE 倍高清图，实现圆角/圆形/文字的抗锯齿。
    """

    def __init__(
        self,
        renderer: "ScheduleRenderer",
        width: float,
        height: float,
        bg: Tuple[int, int, int] = BG,
        scale: float = 3,
        draw: Any = None,
    ):
        self.r = renderer
        self.w = width
        self.h = height
        self.s = scale
        if draw is not None:
            self.img = None
            self.draw = draw
        else:
            self.img = Image.new("RGB", (self.S(width), self.S(height)), bg)
            self.draw = ImageDraw.Draw(self.img)

    @classmethod
    def measurer(cls, renderer: "ScheduleRenderer", draw: Any = None, scale: float = 1) -> "_Canvas":
        if draw is None:
            draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        return cls(renderer, 0, 0, scale=scale, draw=draw)

    # ---------------- 基础换算 ----------------
    def S(self, v: float) -> int:
        return int(round(v * self.s))

    def box(self, b: Tuple[float, float, float, float]) -> Tuple[int, int, int, int]:
        return (self.S(b[0]), self.S(b[1]), self.S(b[2]), self.S(b[3]))

    def font(self, size: float, bold: bool = False) -> Any:
        return self.r._get_font(max(6, int(round(size * self.s))), bold)

    # ---------------- 文字测量 ----------------
    def measure(self, text: str, size: float, bold: bool = False) -> Tuple[float, float]:
        clean = strip_unsupported_chars(text)
        if not clean:
            return 0.0, 0.0
        f = self.font(size, bold)
        try:
            bb = self.draw.textbbox((0, 0), clean, font=f)
            return (bb[2] - bb[0]) / self.s, (bb[3] - bb[1]) / self.s
        except Exception:
            return len(clean) * size * 0.95, size * 1.2

    def tw(self, text: str, size: float, bold: bool = False) -> float:
        return self.measure(text, size, bold)[0]

    def fit(self, text: str, max_w: float, start: int, min_size: int = 14, bold: bool = False) -> int:
        for sz in range(int(start), int(min_size) - 1, -1):
            if self.tw(text, sz, bold) <= max_w:
                return sz
        return int(min_size)

    def wrap(self, text: str, size: float, max_w: float, bold: bool = False) -> List[str]:
        """按像素宽度完整换行（智能保持数字/英文单词与括号完整性，绝不丢弃任何字符）"""
        clean = strip_unsupported_chars(text)
        if not clean:
            return []
        if self.tw(clean, size, bold) <= max_w and "\n" not in clean:
            return [clean]

        # 将连续的英文/数字、前导括号+字符、后置括号与单字拆分为原子词元，避免把教室号 "4101" 拆成 "4" + "101"
        tokens: List[str] = []
        i = 0
        n = len(clean)
        open_brackets = "（(《【["
        close_brackets = "）)》】]·,，。、:："
        while i < n:
            ch = clean[i]
            if ch == "\n":
                tokens.append("\n")
                i += 1
                continue
            if ch.isascii() and (ch.isalnum() or ch in "-_.:"):
                j = i + 1
                while j < n and clean[j].isascii() and (clean[j].isalnum() or clean[j] in "-_.:"):
                    j += 1
                tok = clean[i:j]
                i = j
            elif ch in open_brackets and i + 1 < n and clean[i + 1] != "\n":
                tok = clean[i : i + 2]
                i += 2
            else:
                tok = ch
                i += 1
            while i < n and clean[i] in close_brackets:
                tok += clean[i]
                i += 1
            tokens.append(tok)

        lines: List[str] = []
        current = ""

        def _push_char_by_char(s: str) -> None:
            nonlocal current
            for c in s:
                test_c = current + c
                if not current or self.tw(test_c, size, bold) <= max_w:
                    current = test_c
                else:
                    lines.append(current)
                    current = c

        for tok in tokens:
            if tok == "\n":
                if current:
                    lines.append(current)
                    current = ""
                continue
            test = current + tok
            if not current:
                if self.tw(tok, size, bold) <= max_w:
                    current = tok
                else:
                    _push_char_by_char(tok)
            elif self.tw(test, size, bold) <= max_w:
                current = test
            else:
                lines.append(current)
                current = ""
                if self.tw(tok, size, bold) <= max_w:
                    current = tok
                else:
                    _push_char_by_char(tok)
        if current:
            lines.append(current)
        return lines

    # ---------------- 绘制原语 ----------------
    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: float,
        color: Tuple[int, int, int],
        bold: bool = False,
        anchor: str = "lm",
    ) -> None:
        clean = strip_unsupported_chars(text)
        if not clean:
            return
        f = self.font(size, bold)
        sw = max(1, int(self.s * 0.45)) if (bold and self.r._bold_index == self.r._reg_index) else 0
        try:
            self.draw.text(
                (self.S(x), self.S(y)),
                clean,
                fill=color,
                font=f,
                anchor=anchor,
                stroke_width=sw,
                stroke_fill=color if sw else None,
            )
        except Exception:
            try:
                tw, th = self.measure(clean, size, bold)
                ox = x - (tw / 2 if anchor[0] == "m" else (tw if anchor[0] == "r" else 0))
                oy = y - th / 2
                self.draw.text((self.S(ox), self.S(oy)), clean, fill=color, font=f)
            except Exception:
                pass

    def rect(
        self,
        b: Tuple[float, float, float, float],
        r: float = 0,
        fill: Any = None,
        outline: Any = None,
        width: float = 0,
    ) -> None:
        x1, y1, x2, y2 = b
        if x2 <= x1 or y2 <= y1:
            return
        rr = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
        wpx = max(1, self.S(width)) if (outline is not None and width) else 0
        try:
            if rr > 0:
                self.draw.rounded_rectangle(
                    self.box(b), radius=self.S(rr), fill=fill,
                    outline=outline if wpx else None, width=wpx,
                )
            else:
                self.draw.rectangle(
                    self.box(b), fill=fill, outline=outline if wpx else None, width=wpx
                )
        except Exception:
            try:
                self.draw.rectangle(self.box(b), fill=fill)
            except Exception:
                pass

    def ellipse(self, b: Tuple[float, float, float, float], fill: Any = None) -> None:
        try:
            self.draw.ellipse(self.box(b), fill=fill)
        except Exception:
            pass

    def line(self, pts: List[Tuple[float, float]], color: Tuple[int, int, int], width: float = 1) -> None:
        try:
            self.draw.line(
                [(self.S(px), self.S(py)) for px, py in pts],
                fill=color,
                width=max(1, self.S(width)),
                joint="curve",
            )
        except Exception:
            try:
                self.draw.line([(self.S(px), self.S(py)) for px, py in pts], fill=color, width=max(1, self.S(width)))
            except Exception:
                pass

    def dashed_hline(
        self, x1: float, x2: float, y: float, color: Tuple[int, int, int],
        width: float = 1, dash: float = 8, gap: float = 6,
    ) -> None:
        x = x1
        while x < x2:
            self.line([(x, y), (min(x + dash, x2), y)], color, width)
            x += dash + gap

    def pill(
        self,
        x: float,
        cy: float,
        text: str,
        size: float,
        fg: Tuple[int, int, int],
        bg: Tuple[int, int, int],
        pad: float = 14,
        h: Optional[float] = None,
        bold: bool = True,
        align: str = "left",
        outline: Optional[Tuple[int, int, int]] = None,
    ) -> Tuple[float, float]:
        t_w = self.tw(text, size, bold)
        ph = h or (size + 16)
        if align == "right":
            x2 = x
            x1 = x2 - t_w - pad * 2
        elif align == "center":
            x1 = x - t_w / 2 - pad
            x2 = x + t_w / 2 + pad
        else:
            x1 = x
            x2 = x + t_w + pad * 2
        self.rect((x1, cy - ph / 2, x2, cy + ph / 2), ph / 2, fill=bg, outline=outline, width=2 if outline else 0)
        self.text((x1 + x2) / 2, cy, text, size, fg, bold=bold, anchor="mm")
        return x1, x2

    # ---------------- 高级效果（失败时安全降级） ----------------
    def overlay(
        self, b: Tuple[float, float, float, float], r: float,
        color: Tuple[int, int, int], alpha: float,
    ) -> None:
        if self.img is None:
            return
        try:
            bb = self.box(b)
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            if w <= 0 or h <= 0:
                return
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, w - 1, h - 1), radius=self.S(min(r, (b[2] - b[0]) / 2, (b[3] - b[1]) / 2)),
                fill=int(255 * alpha),
            )
            self.img.paste(color, bb, mask)
        except Exception:
            pass

    def shadow(
        self,
        b: Tuple[float, float, float, float],
        r: float,
        dy: float = 6,
        blur: float = 14,
        alpha: float = 0.10,
        color: Tuple[int, int, int] = (15, 23, 42),
    ) -> None:
        if self.img is None or ImageFilter is None:
            return
        try:
            x1, y1, x2, y2 = b
            pad = blur * 2
            w = self.S(x2 - x1 + pad * 2)
            h = self.S(y2 - y1 + pad * 2)
            mask = Image.new("L", (w, h), 0)
            rr = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
            ImageDraw.Draw(mask).rounded_rectangle(
                (self.S(pad), self.S(pad), self.S(pad + x2 - x1), self.S(pad + y2 - y1)),
                radius=self.S(rr),
                fill=int(255 * alpha),
            )
            mask = mask.filter(ImageFilter.GaussianBlur(self.S(blur) / 2))
            ox = self.S(x1 - pad)
            oy = self.S(y1 - pad + dy)
            self.img.paste(color, (ox, oy, ox + w, oy + h), mask)
        except Exception:
            pass

    def gradient(
        self,
        b: Tuple[float, float, float, float],
        r: float,
        c1: Tuple[int, int, int],
        c2: Tuple[int, int, int],
        vertical: bool = False,
        blobs: Optional[List[Tuple[float, float, float, float]]] = None,
    ) -> None:
        if self.img is None:
            return
        bb = self.box(b)
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        if w <= 0 or h <= 0:
            return
        try:
            tile = Image.new("RGB", (w, h), c1)
            td = ImageDraw.Draw(tile)
            n = h if vertical else w
            step = 2
            for i in range(0, n, step):
                col = _mix(c1, c2, i / max(1, n - 1))
                if vertical:
                    td.rectangle((0, i, w, i + step), fill=col)
                else:
                    td.rectangle((i, 0, i + step, h), fill=col)
            for bx, by, br, ba in blobs or []:
                bm = Image.new("L", (w, h), 0)
                ImageDraw.Draw(bm).ellipse(
                    (self.S(bx - br), self.S(by - br), self.S(bx + br), self.S(by + br)),
                    fill=int(255 * ba),
                )
                tile.paste((255, 255, 255), (0, 0, w, h), bm)
            rr = min(r, (b[2] - b[0]) / 2, (b[3] - b[1]) / 2)
            if rr > 0:
                mask = Image.new("L", (w, h), 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=self.S(rr), fill=255)
                self.img.paste(tile, (bb[0], bb[1]), mask)
            else:
                self.img.paste(tile, (bb[0], bb[1]))
        except Exception:
            self.rect(b, r, fill=c1)

    def avatar(
        self,
        path: Optional[Path],
        name: str,
        x: float,
        y: float,
        size: float,
        ring: Optional[Tuple[int, int, int]] = (255, 255, 255),
        ring_w: float = 4,
        dot: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        if self.img is None:
            return
        try:
            if ring is not None and ring_w > 0:
                self.ellipse((x - ring_w, y - ring_w, x + size + ring_w, y + size + ring_w), fill=ring)
            px = self.S(size)
            av = None
            if path and Path(path).exists():
                try:
                    raw = Image.open(path).convert("RGBA").resize((px, px), _lanczos())
                    av = Image.new("RGB", (px, px), (255, 255, 255))
                    av.paste(raw, (0, 0), raw)
                except Exception:
                    av = None
            if av is None:
                main_rgb, _, _ = get_vivid_theme(name)
                av = Image.new("RGB", (px, px), main_rgb)
                ad = ImageDraw.Draw(av)
                ch = (strip_unsupported_chars(name) or "友")[0].upper()
                f = self.r._get_font(max(8, int(px * 0.46)), True)
                try:
                    ad.text((px // 2, px // 2), ch, fill=(255, 255, 255), font=f, anchor="mm")
                except Exception:
                    ad.text((px // 4, px // 4), ch, fill=(255, 255, 255), font=f)
            mask = Image.new("L", (px, px), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, px - 1, px - 1), fill=255)
            self.img.paste(av, (self.S(x), self.S(y)), mask)
            if dot is not None:
                d = max(18, size * 0.26)
                dx = x + size - d + 2
                dy = y + size - d + 2
                self.ellipse((dx - 3, dy - 3, dx + d + 3, dy + d + 3), fill=(255, 255, 255))
                self.ellipse((dx, dy, dx + d, dy + d), fill=dot)
        except Exception:
            pass

    # ---------------- 输出 ----------------
    def save(self, path: Path) -> None:
        if self.img is None:
            return
        out_w = max(1, int(round(self.w * OUT_SCALE)))
        out_h = max(1, int(round(self.h * OUT_SCALE)))
        try:
            out = self.img.resize((out_w, out_h), _lanczos())
        except Exception:
            out = self.img
        out.save(path, format="PNG")
