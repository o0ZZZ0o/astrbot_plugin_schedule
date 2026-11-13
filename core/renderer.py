from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.request
import ssl
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
    now_in_tz,
)
from .storage import StorageManager, safe_filename

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    HAS_PIL = True
except ImportError:
    HAS_PIL = False


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

# Debian 官方及国内高速镜像站的文泉驿微米黑字体 deb 包（用于在极简无中文字体的 Debian 服务器上自动离线提取）
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


def get_vivid_theme(course_name: str, index: int = 0) -> Tuple[Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]]:
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
    """
    移除标准中文字体不支持的彩色 Emoji 字符（避免在 Linux Pillow 下渲染为 □ 方框），
    保留全部中英文、数字、全角/半角标点及常用几何符号。
    """
    if not text:
        return ""
    cleaned: List[str] = []
    for ch in text:
        cp = ord(ch)
        # 过滤 Unicode 补充平面 Emoji (U+1F000 - U+1FAFF) 及杂项符号区 Emoji + 变体选择符
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
    专为手机 QQ 聊天窗口优化的高清竖版课程表渲染引擎：
    - 黄金竖版比例 (720px 宽，点开前在手机聊天流中直接满宽清晰展示)
    - 超大字号 + 粗细双字重层次，无需点开原图即可一眼看清时间、课程名、教室
    - 4x 超采样抗锯齿圆形头像与徽章，彻底告别锯齿边缘
    - 内置 Debian Linux 中文字体校验与自动修复机制，彻底杜绝中文乱码方框 (□□□)
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
        """
        严格校验字体文件是否真正包含中文字形：
        若字体不含中文，'课' 与 '程' 均会回退到同一个 .notdef 方框字形，其位图完全相同；
        只有真正包含中文的字体，'课' 与 '程' 才会渲染出不同的字形位图。
        """
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
        # 1. 最优先检查插件自带 fonts/ 与数据目录 fonts/（命中即刻返回，耗时 <1ms）
        for folder in (self.plugin_dir / "fonts", self.storage.data_dir / "fonts"):
            if folder.exists():
                for ext in ("*.ttc", "*.ttf", "*.otf"):
                    for p in sorted(folder.glob(ext)):
                        if self._try_use_font_candidate(str(p)):
                            return

        # 2. 检查预设常见系统字体路径
        for p_str in FONT_SEARCH_PATHS:
            if self._try_use_font_candidate(p_str):
                return

        # 3. 在 Debian/Linux 上通过 fc-list :lang=zh 扫描
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

        # 4. 递归扫描 Linux 字体目录
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

        # 5. 若当前 Debian 服务器完全没有安装中文字体，自动从国内镜像拉取并解压 wqy-microhei.ttc
        downloaded = self._auto_provision_debian_cjk_font()
        if downloaded:
            self._try_use_font_candidate(downloaded)

    def _auto_provision_debian_cjk_font(self) -> Optional[str]:
        """当 Debian Linux 服务器缺少中文字体时，自动从国内镜像拉取并解压 wqy-microhei.ttc"""
        target_dir = self.storage.data_dir / "fonts"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_ttc = target_dir / "wqy-microhei.ttc"
        if target_ttc.exists() and target_ttc.stat().st_size > 100000:
            return str(target_ttc)

        # 尝试从国内 Debian 镜像下载 fonts-wqy-microhei deb 包并用 dpkg-deb 解压
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

    def _text_size(self, draw: Any, text: str, font: Any) -> Tuple[int, int]:
        if not HAS_PIL or draw is None:
            return len(text) * 18, 26
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            return int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])
        except Exception:
            return len(text) * 18, 26

    def _draw_text(
        self,
        draw: Any,
        xy: Tuple[int, int],
        text: str,
        fill: Tuple[int, int, int],
        font: Any,
        bold_stroke: bool = False,
    ) -> None:
        """安全绘制文本（自动过滤不兼容 Emoji，并支持对单字重字体附加轻微描边加粗）"""
        if not HAS_PIL or draw is None:
            return
        clean = strip_unsupported_chars(text)
        if not clean:
            return
        stroke_w = 1 if (bold_stroke and self._bold_index == self._reg_index) else 0
        try:
            draw.text(
                xy,
                clean,
                fill=fill,
                font=font,
                stroke_width=stroke_w,
                stroke_fill=fill if stroke_w > 0 else None,
            )
        except Exception:
            draw.text(xy, clean, fill=fill, font=font)

    def _truncate_text(self, draw: Any, text: str, font: Any, max_width: int) -> str:
        clean = strip_unsupported_chars(text)
        if not clean:
            return ""
        w, _ = self._text_size(draw, clean, font)
        if w <= max_width:
            return clean
        for i in range(len(clean), 0, -1):
            sub = clean[:i] + "…"
            sw, _ = self._text_size(draw, sub, font)
            if sw <= max_width:
                return sub
        return clean[:1]

    def _wrap_text(
        self, draw: Any, text: str, font: Any, max_width: int, max_lines: int = 2
    ) -> List[str]:
        clean = strip_unsupported_chars(text)
        if not clean:
            return []
        lines: List[str] = []
        current = ""
        for ch in clean:
            test = current + ch
            w, _ = self._text_size(draw, test, font)
            if w <= max_width or not current:
                current = test
            else:
                lines.append(current)
                current = ch
                if len(lines) >= max_lines:
                    break
        if current and len(lines) < max_lines:
            lines.append(current)
        return lines

    def _draw_avatar(
        self,
        canvas: Any,
        avatar_path: Optional[Path],
        display_name: str,
        x: int,
        y: int,
        size: int,
        ring_color: Tuple[int, int, int] = (255, 255, 255),
        ring_width: int = 4,
        status_dot_color: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        """使用 4x 超采样绘制边缘丝滑抗锯齿的圆形 QQ 头像与状态圆点"""
        if not HAS_PIL:
            return
        try:
            scale = 4
            total_size = size + ring_width * 2
            hi_total = total_size * scale
            hi_size = size * scale
            hi_ring = ring_width * scale

            # 1. 绘制超采样外环
            ring_layer = Image.new("RGBA", (hi_total, hi_total), (0, 0, 0, 0))
            ring_draw = ImageDraw.Draw(ring_layer)
            ring_draw.ellipse((0, 0, hi_total - 1, hi_total - 1), fill=(*ring_color, 255))
            smooth_ring = ring_layer.resize(
                (total_size, total_size), Image.Resampling.LANCZOS
            )
            canvas.paste(smooth_ring, (x - ring_width, y - ring_width), smooth_ring)

            # 2. 准备头像内容
            avatar_img = None
            if avatar_path and Path(avatar_path).exists():
                try:
                    raw_av = Image.open(avatar_path).convert("RGBA")
                    avatar_img = raw_av.resize((size, size), Image.Resampling.LANCZOS)
                except Exception:
                    avatar_img = None

            if avatar_img is None:
                # 兜底生成高颜值首字头像
                primary_rgb, _, _ = get_vivid_theme(display_name)
                avatar_img = Image.new("RGBA", (size, size), (*primary_rgb, 255))
                av_draw = ImageDraw.Draw(avatar_img)
                clean_name = strip_unsupported_chars(display_name) or "友"
                first_char = clean_name[0].upper()
                f_av = self._get_font(int(size * 0.48), bold=True)
                tw, th = self._text_size(av_draw, first_char, f_av)
                self._draw_text(
                    av_draw,
                    ((size - tw) // 2, (size - th) // 2 - int(size * 0.06)),
                    first_char,
                    (255, 255, 255),
                    f_av,
                    bold_stroke=True,
                )

            # 3. 4x 超采样圆形遮罩裁剪
            hi_mask = Image.new("L", (hi_size, hi_size), 0)
            hi_mask_draw = ImageDraw.Draw(hi_mask)
            hi_mask_draw.ellipse((0, 0, hi_size - 1, hi_size - 1), fill=255)
            smooth_mask = hi_mask.resize((size, size), Image.Resampling.LANCZOS)
            canvas.paste(avatar_img, (x, y), smooth_mask)

            # 4. 可选状态指示圆点（同样 4x 超采样抗锯齿）
            if status_dot_color is not None:
                dot_d = max(22, int(size * 0.26))
                dot_pad = 4
                full_d = dot_d + dot_pad * 2
                hi_full_d = full_d * scale
                dot_layer = Image.new("RGBA", (hi_full_d, hi_full_d), (0, 0, 0, 0))
                d_draw = ImageDraw.Draw(dot_layer)
                d_draw.ellipse(
                    (0, 0, hi_full_d - 1, hi_full_d - 1), fill=(255, 255, 255, 255)
                )
                d_draw.ellipse(
                    (
                        dot_pad * scale,
                        dot_pad * scale,
                        (full_d - dot_pad) * scale - 1,
                        (full_d - dot_pad) * scale - 1,
                    ),
                    fill=(*status_dot_color, 255),
                )
                smooth_dot = dot_layer.resize((full_d, full_d), Image.Resampling.LANCZOS)
                dx = x + size - full_d + 4
                dy = y + size - full_d + 4
                canvas.paste(smooth_dot, (dx, dy), smooth_dot)
        except Exception:
            pass

    def _draw_course_card(
        self,
        draw: Any,
        c: CourseInstance,
        idx: int,
        x: int,
        y: int,
        w: int,
        h: int,
    ) -> None:
        """绘制单张大字号、高对比度的课程卡片（点开前在手机缩略图中即可看清）"""
        f_time = self._get_font(32, bold=True)
        f_node = self._get_font(21, bold=True)
        f_status = self._get_font(22, bold=True)
        f_course = self._get_font(36, bold=True)
        f_meta = self._get_font(25, bold=True)
        f_teacher = self._get_font(24, bold=False)

        vivid_main, vivid_bg, vivid_dark = get_vivid_theme(c.course_name, idx)

        if c.status == "ongoing":
            card_bg = (236, 253, 245)       # 鲜明薄荷绿底
            border_col = (16, 185, 129)     # 翠绿边框
            bar_col = (5, 150, 105)
            pill_bg = (5, 150, 105)
            pill_fg = (255, 255, 255)
            time_col = (6, 78, 59)
            title_col = (6, 78, 59)
            room_bg = (209, 250, 229)
            room_fg = (4, 120, 87)
            status_label = f"进行中 · 剩{format_duration_cn(c.wait_or_remain_minutes)}"
        elif c.status == "next":
            card_bg = (239, 246, 255)       # 鲜明晴空蓝底
            border_col = (59, 130, 246)     # 亮蓝边框
            bar_col = (37, 99, 235)
            pill_bg = (37, 99, 235)
            pill_fg = (255, 255, 255)
            time_col = (30, 58, 138)
            title_col = (15, 23, 42)
            room_bg = (219, 234, 254)
            room_fg = (29, 78, 216)
            status_label = f"下一节 · {format_duration_cn(c.wait_or_remain_minutes)}后"
        elif c.status == "finished":
            card_bg = (255, 255, 255)
            border_col = (226, 232, 240)
            bar_col = (148, 163, 184)
            pill_bg = (241, 245, 249)
            pill_fg = (100, 116, 139)
            time_col = (100, 116, 139)
            title_col = (71, 85, 105)
            room_bg = (241, 245, 249)
            room_fg = (100, 116, 139)
            status_label = "已下课"
        else:
            card_bg = (255, 255, 255)
            border_col = (226, 232, 240)
            bar_col = vivid_main
            pill_bg = vivid_bg
            pill_fg = vivid_dark
            time_col = (15, 23, 42)
            title_col = (15, 23, 42)
            room_bg = vivid_bg
            room_fg = vivid_dark
            status_label = f"{c.start_time} 开课"

        # 卡片圆角底板
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=24,
            fill=card_bg,
            outline=border_col,
            width=3 if c.status in ("ongoing", "next") else 2,
        )
        # 左侧鲜明色带
        draw.rounded_rectangle(
            (x, y + 14, x + 12, y + h - 14),
            radius=6,
            fill=bar_col,
        )

        # 第一行：时间段 (32px Bold) + 节次胶囊 + 右上角状态胶囊
        time_str = f"{c.start_time} - {c.end_time}"
        self._draw_text(
            draw, (x + 32, y + 20), time_str, time_col, f_time, bold_stroke=True
        )
        tw, _ = self._text_size(draw, time_str, f_time)

        node_txt = c.node_range_str
        nw, _ = self._text_size(draw, node_txt, f_node)
        nx = x + 32 + tw + 16
        draw.rounded_rectangle(
            (nx, y + 23, nx + nw + 22, y + 55),
            radius=10,
            fill=room_bg,
        )
        self._draw_text(
            draw, (nx + 11, y + 27), node_txt, room_fg, f_node, bold_stroke=True
        )

        # 右上角状态胶囊
        sw, _ = self._text_size(draw, status_label, f_status)
        sx2 = x + w - 24
        sx1 = sx2 - sw - 28
        draw.rounded_rectangle(
            (sx1, y + 20, sx2, y + 58),
            radius=19,
            fill=pill_bg,
        )
        self._draw_text(
            draw, (sx1 + 14, y + 26), status_label, pill_fg, f_status, bold_stroke=True
        )

        # 第二行：超大课程名称 (36px Bold)
        course_title = self._truncate_text(draw, c.course_name, f_course, w - 64)
        self._draw_text(
            draw, (x + 32, y + 72), course_title, title_col, f_course, bold_stroke=True
        )

        # 第三行：教室胶囊 (25px Bold) + 任课教师 (24px)
        room_str = f"教室 · {c.room or '未指定'}"
        room_str = self._truncate_text(draw, room_str, f_meta, 340)
        rw, _ = self._text_size(draw, room_str, f_meta)
        ry1 = y + 126
        ry2 = y + 164
        draw.rounded_rectangle(
            (x + 32, ry1, x + 32 + rw + 26, ry2),
            radius=12,
            fill=room_bg,
        )
        self._draw_text(
            draw, (x + 45, ry1 + 6), room_str, room_fg, f_meta, bold_stroke=True
        )

        if c.teacher:
            t_str = self._truncate_text(draw, f"教师 · {c.teacher}", f_teacher, 220)
            self._draw_text(
                draw,
                (x + 32 + rw + 44, ry1 + 7),
                t_str,
                (100, 116, 139),
                f_teacher,
            )

    # =========================================================================
    # 样式一（A）：个人单日课表（竖版大字卡片风，适配手机未点开直接预览）
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

        # 关键体验优化：如果用户触发 /查看课表 时今日课程已全部下课，仍然展示今日已上完的课程，
        # 同时在下方附上「明日课程预览」，绝不让卡片变成空洞的白框！
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

        width = 720
        pad_x = 28
        card_w = width - pad_x * 2
        hero_h = 268
        course_card_h = 182
        course_gap = 18

        # 计算总高度，确保至少为竖屏比例 (>= 860px)
        content_h = 28 + hero_h + 24
        if only_upcoming and all_courses and not upcoming_courses:
            content_h += 82  # “今日课程已全部结束”横幅
        if show_courses:
            content_h += len(show_courses) * (course_card_h + course_gap)
        else:
            content_h += 240  # 当天无课海报卡

        if show_tomorrow:
            content_h += 68 + len(show_tomorrow) * (course_card_h + course_gap)

        height = max(860, content_h + 70)

        img = Image.new("RGB", (width, height), (242, 244, 248))
        draw = ImageDraw.Draw(img)

        f_hero_name = self._get_font(34, bold=True)
        f_hero_sub = self._get_font(22, bold=False)
        f_tag = self._get_font(24, bold=True)
        f_stat_val = self._get_font(34, bold=True)
        f_stat_lbl = self._get_font(20, bold=False)
        f_sec_title = self._get_font(28, bold=True)
        f_empty_big = self._get_font(36, bold=True)
        f_empty_sub = self._get_font(24, bold=False)
        f_footer = self._get_font(20, bold=False)

        # 1. 顶部 Bento Hero 卡片（深邃皇家靛蓝卡片）
        hx1, hy1 = pad_x, 28
        hx2, hy2 = width - pad_x, 28 + hero_h
        draw.rounded_rectangle(
            (hx1, hy1, hx2, hy2),
            radius=28,
            fill=(30, 27, 75),
        )
        # 顶部高亮装饰条
        draw.rounded_rectangle(
            (hx1 + 28, hy1, hx1 + 160, hy1 + 8),
            radius=4,
            fill=(99, 102, 241),
        )

        # 绘制丝滑抗锯齿圆形 QQ 头像
        self._draw_avatar(
            img,
            avatar_path,
            display_name,
            x=hx1 + 28,
            y=hy1 + 28,
            size=96,
            ring_color=(99, 102, 241),
            ring_width=4,
        )

        # 群昵称与日期周次
        name_txt = self._truncate_text(draw, display_name, f_hero_name, 330)
        self._draw_text(
            draw, (hx1 + 144, hy1 + 32), name_txt, (255, 255, 255), f_hero_name, bold_stroke=True
        )
        date_txt = f"{target_date.strftime('%m月%d日')} {weekday_str} · 第 {week_num} 周"
        self._draw_text(
            draw, (hx1 + 144, hy1 + 84), date_txt, (199, 210, 254), f_hero_sub
        )

        # 右上角视图胶囊标签
        tw, _ = self._text_size(draw, title_prefix, f_tag)
        tx2 = hx2 - 28
        tx1 = tx2 - tw - 32
        draw.rounded_rectangle(
            (tx1, hy1 + 32, tx2, hy1 + 80),
            radius=24,
            fill=(79, 70, 229),
        )
        self._draw_text(
            draw, (tx1 + 16, hy1 + 42), title_prefix, (255, 255, 255), f_tag, bold_stroke=True
        )

        # Hero 底部三栏 Bento 数据块
        bento_items = [
            ("今日课程", f"{len(all_courses)} 门"),
            ("总计节次", f"{total_nodes} 节"),
            ("课堂时长", f"{total_hours} 小时"),
        ]
        bento_w = (card_w - 56 - 24) // 3
        for b_idx, (b_lbl, b_val) in enumerate(bento_items):
            bx = hx1 + 28 + b_idx * (bento_w + 12)
            by = hy1 + 148
            draw.rounded_rectangle(
                (bx, by, bx + bento_w, by + 94),
                radius=18,
                fill=(49, 46, 129),
            )
            self._draw_text(draw, (bx + 20, by + 14), b_lbl, (165, 180, 252), f_stat_lbl)
            self._draw_text(
                draw, (bx + 20, by + 44), b_val, (255, 255, 255), f_stat_val, bold_stroke=True
            )

        y_cursor = hy2 + 24

        # 如果今日有课但已全部结束，先绘制一条鲜明的绿色提示横幅
        if only_upcoming and all_courses and not upcoming_courses:
            draw.rounded_rectangle(
                (pad_x, y_cursor, width - pad_x, y_cursor + 64),
                radius=18,
                fill=(209, 250, 229),
                outline=(16, 185, 129),
                width=2,
            )
            done_msg = "今日课程已全部下课 · 以下为今日已上课程回顾"
            dmw, _ = self._text_size(draw, done_msg, f_tag)
            self._draw_text(
                draw,
                ((width - dmw) // 2, y_cursor + 17),
                done_msg,
                (6, 95, 70),
                f_tag,
                bold_stroke=True,
            )
            y_cursor += 82

        # 2. 绘制当日课程卡片列表或无课大字海报卡
        if show_courses:
            for idx, c in enumerate(show_courses):
                self._draw_course_card(
                    draw, c, idx, x=pad_x, y=y_cursor, w=card_w, h=course_card_h
                )
                y_cursor += course_card_h + course_gap
        else:
            empty_box_h = 220 if show_tomorrow else (height - y_cursor - 84)
            draw.rounded_rectangle(
                (pad_x, y_cursor, width - pad_x, y_cursor + empty_box_h),
                radius=26,
                fill=(255, 255, 255),
                outline=(226, 232, 240),
                width=2,
            )
            # 装饰几何色块
            mid_y = y_cursor + empty_box_h // 2
            t1 = "当天无任何课程安排"
            t2 = "全天自由支配时间 · 尽情休息或约群友吧"
            w1, _ = self._text_size(draw, t1, f_empty_big)
            w2, _ = self._text_size(draw, t2, f_empty_sub)
            self._draw_text(
                draw,
                ((width - w1) // 2, mid_y - 36),
                t1,
                (15, 23, 42),
                f_empty_big,
                bold_stroke=True,
            )
            self._draw_text(
                draw,
                ((width - w2) // 2, mid_y + 18),
                t2,
                (100, 116, 139),
                f_empty_sub,
            )
            y_cursor += empty_box_h + 20

        # 3. 明日课程预览（当今日无剩余课程时自动展示）
        if show_tomorrow:
            self._draw_text(
                draw,
                (pad_x + 8, y_cursor + 12),
                f"明日课程预览（共 {len(show_tomorrow)} 门课）",
                (49, 46, 129),
                f_sec_title,
                bold_stroke=True,
            )
            y_cursor += 58
            for idx, tc in enumerate(show_tomorrow):
                self._draw_course_card(
                    draw,
                    tc,
                    idx + 3,
                    x=pad_x,
                    y=y_cursor,
                    w=card_w,
                    h=course_card_h,
                )
                y_cursor += course_card_h + course_gap

        # 底部简约时间签名
        footer_str = f"AstrBot 课程表  ·  {now.strftime('%Y-%m-%d %H:%M')}"
        fw, _ = self._text_size(draw, footer_str, f_footer)
        self._draw_text(
            draw,
            ((width - fw) // 2, height - 44),
            footer_str,
            (148, 163, 184),
            f_footer,
        )

        img.save(out_path, format="PNG")
        return out_path

    # =========================================================================
    # 样式一（B）：个人周课表网格（自适应列宽 + 大字鲜明色块）
    # =========================================================================
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
                create_minimal_png(920, 1080, (244, 246, 250), (49, 46, 129), 160)
            )
            return out_path

        # 自适应节次行数（至少展示 10 节，若有晚课则扩展）
        max_nodes = 10
        for day_list in week_courses.values():
            for c in day_list:
                if c.end_node > max_nodes:
                    max_nodes = c.end_node

        # 自适应天数：若周六、周日无课则展示 5 列（让每列宽 40%，字体更大更清晰），若有周末课则展示 7 列
        has_weekend = bool(week_courses.get(6) or week_courses.get(7))
        num_days = 7 if has_weekend else 5

        width = 920
        pad_x = 24
        hero_h = 156
        col_header_h = 78
        left_axis_w = 104
        row_h = 92
        grid_w = width - pad_x * 2 - left_axis_w
        col_w = grid_w // num_days
        height = 24 + hero_h + 20 + col_header_h + max_nodes * row_h + 64

        img = Image.new("RGB", (width, height), (242, 244, 248))
        draw = ImageDraw.Draw(img)

        f_hero_title = self._get_font(34, bold=True)
        f_hero_sub = self._get_font(22, bold=False)
        f_col_wd = self._get_font(24, bold=True)
        f_col_dt = self._get_font(18, bold=False)
        f_node_num = self._get_font(22, bold=True)
        f_node_tm = self._get_font(16, bold=False)
        f_cell_name = self._get_font(22 if num_days == 5 else 19, bold=True)
        f_cell_room = self._get_font(19 if num_days == 5 else 16, bold=True)

        # 顶部 Hero 卡片
        hx1, hy1 = pad_x, 24
        hx2, hy2 = width - pad_x, 24 + hero_h
        draw.rounded_rectangle((hx1, hy1, hx2, hy2), radius=26, fill=(30, 27, 75))

        self._draw_avatar(
            img,
            avatar_path,
            display_name,
            x=hx1 + 26,
            y=hy1 + 28,
            size=100,
            ring_color=(99, 102, 241),
            ring_width=4,
        )

        all_wk_instances = [c for dlist in week_courses.values() for c in dlist]
        total_wk_nodes = sum(c.step for c in all_wk_instances)
        total_wk_hours = round(sum(c.duration_minutes for c in all_wk_instances) / 60.0, 1)

        head_str = self._truncate_text(
            draw, f"{display_name} · {title_prefix}（第 {week_num} 周）", f_hero_title, width - 220
        )
        self._draw_text(
            draw, (hx1 + 148, hy1 + 32), head_str, (255, 255, 255), f_hero_title, bold_stroke=True
        )
        sub_str = (
            f"{monday.strftime('%m/%d')} - {sunday.strftime('%m/%d')}   ·   共 {len(all_wk_instances)} 门次 / {total_wk_nodes} 节 / {total_wk_hours} 小时"
        )
        self._draw_text(draw, (hx1 + 148, hy1 + 88), sub_str, (199, 210, 254), f_hero_sub)

        # 课表矩阵白底外框
        grid_top = hy2 + 20
        grid_bottom = grid_top + col_header_h + max_nodes * row_h
        draw.rounded_rectangle(
            (pad_x, grid_top, width - pad_x, grid_bottom),
            radius=24,
            fill=(255, 255, 255),
            outline=(226, 232, 240),
            width=2,
        )

        # 表头背景
        draw.rounded_rectangle(
            (pad_x, grid_top, width - pad_x, grid_top + col_header_h),
            radius=24,
            fill=(248, 250, 252),
        )
        self._draw_text(
            draw,
            (pad_x + 26, grid_top + 24),
            "节次",
            (71, 85, 105),
            f_col_wd,
            bold_stroke=True,
        )

        today = now.date()
        body_top = grid_top + col_header_h

        for d_idx in range(num_days):
            cur_d = monday + timedelta(days=d_idx)
            cx1 = pad_x + left_axis_w + d_idx * col_w
            cx2 = cx1 + col_w
            is_today = cur_d == today
            if is_today:
                draw.rectangle(
                    (cx1, body_top, cx2, grid_bottom),
                    fill=(245, 247, 255),
                )
                draw.rounded_rectangle(
                    (cx1 + 6, grid_top + 8, cx2 - 6, grid_top + col_header_h - 8),
                    radius=16,
                    fill=(79, 70, 229),
                )
            wd_txt = WEEKDAY_NAMES[d_idx]
            dt_txt = cur_d.strftime("%m/%d")
            fg1 = (255, 255, 255) if is_today else (15, 23, 42)
            fg2 = (224, 231, 255) if is_today else (100, 116, 139)

            w1, _ = self._text_size(draw, wd_txt, f_col_wd)
            w2, _ = self._text_size(draw, dt_txt, f_col_dt)
            self._draw_text(
                draw,
                (cx1 + (col_w - w1) // 2, grid_top + 14),
                wd_txt,
                fg1,
                f_col_wd,
                bold_stroke=True,
            )
            self._draw_text(
                draw, (cx1 + (col_w - w2) // 2, grid_top + 46), dt_txt, fg2, f_col_dt
            )

        # 水平与垂直分隔线 + 左侧节次时间
        slot_map = {s.node: s for s in schedule.time_slots}
        default_map = {s["node"]: s for s in DEFAULT_TIME_SLOTS}

        for node in range(1, max_nodes + 1):
            ry1 = body_top + (node - 1) * row_h
            ry2 = ry1 + row_h
            draw.line((pad_x, ry1, width - pad_x, ry1), fill=(226, 232, 240), width=1)

            slot = slot_map.get(node)
            st_str = slot.start_time if slot else default_map.get(node, {}).get("start_time", "")
            et_str = slot.end_time if slot else default_map.get(node, {}).get("end_time", "")

            n_str = f"{node}"
            nw, _ = self._text_size(draw, n_str, f_node_num)
            self._draw_text(
                draw,
                (pad_x + (left_axis_w - nw) // 2, ry1 + 14),
                n_str,
                (30, 41, 59),
                f_node_num,
                bold_stroke=True,
            )
            if st_str:
                tw, _ = self._text_size(draw, st_str, f_node_tm)
                self._draw_text(
                    draw,
                    (pad_x + (left_axis_w - tw) // 2, ry1 + 44),
                    st_str,
                    (100, 116, 139),
                    f_node_tm,
                )
            if et_str:
                tw, _ = self._text_size(draw, et_str, f_node_tm)
                self._draw_text(
                    draw,
                    (pad_x + (left_axis_w - tw) // 2, ry1 + 64),
                    et_str,
                    (148, 163, 184),
                    f_node_tm,
                )

        for d_idx in range(num_days + 1):
            cx = pad_x + left_axis_w + d_idx * col_w
            draw.line((cx, grid_top, cx, grid_bottom), fill=(226, 232, 240), width=1)

        # 绘制课程鲜明色块
        for wd, day_courses in week_courses.items():
            d_idx = wd - 1
            if not (0 <= d_idx < num_days):
                continue
            cx1 = pad_x + left_axis_w + d_idx * col_w + 5
            cx2 = cx1 + col_w - 10

            for idx, c in enumerate(day_courses):
                s_node = max(1, min(max_nodes, c.start_node))
                e_node = max(s_node, min(max_nodes, c.end_node))
                cy1 = body_top + (s_node - 1) * row_h + 5
                cy2 = body_top + e_node * row_h - 5

                v_main, v_bg, v_dark = get_vivid_theme(c.course_name, idx)

                draw.rounded_rectangle(
                    (cx1, cy1, cx2, cy2),
                    radius=14,
                    fill=v_bg,
                    outline=v_main,
                    width=2,
                )
                draw.rounded_rectangle(
                    (cx1, cy1, cx2, min(cy2, cy1 + 8)),
                    radius=4,
                    fill=v_main,
                )

                inner_w = (cx2 - cx1) - 16
                c_lines = self._wrap_text(draw, c.course_name, f_cell_name, inner_w, max_lines=3)
                ty = cy1 + 14
                for ln in c_lines:
                    if ty + 24 > cy2 - 6:
                        break
                    self._draw_text(
                        draw, (cx1 + 8, ty), ln, v_dark, f_cell_name, bold_stroke=True
                    )
                    ty += 26

                if c.room and ty + 20 <= cy2 - 4:
                    r_lines = self._wrap_text(
                        draw, f"@{c.room}", f_cell_room, inner_w, max_lines=2
                    )
                    for rln in r_lines:
                        if ty + 20 > cy2 - 4:
                            break
                        self._draw_text(
                            draw, (cx1 + 8, ty + 4), rln, v_main, f_cell_room, bold_stroke=True
                        )
                        ty += 22

        footer_str = f"AstrBot 课程表  ·  第 {week_num} 周  ·  {now.strftime('%Y-%m-%d %H:%M')}"
        fw, _ = self._text_size(draw, footer_str, f_col_dt)
        self._draw_text(
            draw, ((width - fw) // 2, height - 42), footer_str, (148, 163, 184), f_col_dt
        )

        img.save(out_path, format="PNG")
        return out_path

    # =========================================================================
    # 样式二：群友课表总览（鲜明社交状态看板，720px 竖版大字卡片）
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

        rows: List[Dict[str, Any]] = []
        ongoing_count = 0
        free_count = 0
        has_class_tomorrow_count = 0

        for prof, sched, av_path in members_data:
            day_courses = sched.get_courses_for_date(target_date, now=now, tz_name=tz_name)
            ongoing_c = next((c for c in day_courses if c.status == "ongoing"), None)
            next_c = next((c for c in day_courses if c.status in ("next", "upcoming")), None)

            if is_tomorrow:
                if day_courses:
                    has_class_tomorrow_count += 1
                    sort_rank = (0, day_courses[0].start_time, -len(day_courses))
                else:
                    free_count += 1
                    sort_rank = (1, "99:99", 0)
            else:
                if ongoing_c:
                    ongoing_count += 1
                    sort_rank = (0, ongoing_c.end_time, 0)
                elif next_c:
                    sort_rank = (1, next_c.start_time, 0)
                else:
                    free_count += 1
                    sort_rank = (2, "99:99", 0)

            rows.append(
                {
                    "profile": prof,
                    "display_name": prof.get_display_name(group_id),
                    "avatar_path": av_path,
                    "day_courses": day_courses,
                    "ongoing": ongoing_c,
                    "next": next_c,
                    "sort_rank": sort_rank,
                }
            )

        rows.sort(key=lambda r: r["sort_rank"])

        width = 720
        pad_x = 28
        card_w = width - pad_x * 2
        hero_h = 256
        member_card_h = 216
        card_gap = 20

        content_h = 28 + hero_h + 24 + len(rows) * (member_card_h + card_gap)
        height = max(860, content_h + 68)

        img = Image.new("RGB", (width, height), (240, 246, 246))
        draw = ImageDraw.Draw(img)

        f_hero_title = self._get_font(38, bold=True)
        f_hero_sub = self._get_font(22, bold=False)
        f_stat_val = self._get_font(36, bold=True)
        f_stat_lbl = self._get_font(20, bold=False)
        f_name = self._get_font(32, bold=True)
        f_pill = self._get_font(22, bold=True)
        f_main_line = self._get_font(27, bold=True)
        f_sub_line = self._get_font(23, bold=False)
        f_footer = self._get_font(20, bold=False)

        # 1. 顶部社交状态看板 Hero 卡片（深青墨绿 + 亮青色块）
        hx1, hy1 = pad_x, 28
        hx2, hy2 = width - pad_x, 28 + hero_h
        draw.rounded_rectangle((hx1, hy1, hx2, hy2), radius=28, fill=(17, 36, 42))
        draw.rounded_rectangle(
            (hx1 + 28, hy1, hx1 + 170, hy1 + 8), radius=4, fill=(20, 184, 166)
        )

        title_str = "群友明日课表总览" if is_tomorrow else "群友实时上课状态"
        self._draw_text(
            draw, (hx1 + 28, hy1 + 28), title_str, (255, 255, 255), f_hero_title, bold_stroke=True
        )
        sub_str = f"{target_date.strftime('%Y年%m月%d日')} {weekday_str}  ·  查询时间 {now.strftime('%H:%M')}"
        self._draw_text(draw, (hx1 + 28, hy1 + 84), sub_str, (153, 246, 228), f_hero_sub)

        if is_tomorrow:
            stats = [
                ("已绑群友", f"{len(rows)} 人", (20, 184, 166)),
                ("明日有课", f"{has_class_tomorrow_count} 人", (59, 130, 246)),
                ("明日无课", f"{free_count} 人", (16, 185, 129)),
            ]
        else:
            stats = [
                ("已绑群友", f"{len(rows)} 人", (20, 184, 166)),
                ("正在上课", f"{ongoing_count} 人", (249, 115, 22)),
                ("空闲可约", f"{free_count} 人", (16, 185, 129)),
            ]

        bento_w = (card_w - 56 - 24) // 3
        for s_idx, (s_lbl, s_val, s_col) in enumerate(stats):
            bx = hx1 + 28 + s_idx * (bento_w + 12)
            by = hy1 + 138
            draw.rounded_rectangle(
                (bx, by, bx + bento_w, by + 94), radius=18, fill=(26, 54, 62)
            )
            self._draw_text(draw, (bx + 18, by + 14), s_lbl, (153, 246, 228), f_stat_lbl)
            self._draw_text(
                draw, (bx + 18, by + 44), s_val, s_col, f_stat_val, bold_stroke=True
            )

        y_cursor = hy2 + 24

        # 2. 每位群友的大字状态卡片
        for row in rows:
            dname = row["display_name"]
            av_path = row["avatar_path"]
            day_courses: List[CourseInstance] = row["day_courses"]
            ongoing_c: Optional[CourseInstance] = row["ongoing"]
            next_c: Optional[CourseInstance] = row["next"]

            c_top = y_cursor
            c_bot = y_cursor + member_card_h

            if is_tomorrow:
                if day_courses:
                    accent_col = (37, 99, 235)
                    box_bg = (239, 246, 255)
                    box_fg = (30, 58, 138)
                    total_nodes = sum(c.step for c in day_courses)
                    early_str = " · 早八" if day_courses[0].start_time <= "08:30" else ""
                    pill_txt = f"明日 {len(day_courses)} 门 ({total_nodes}节){early_str}"
                else:
                    accent_col = (16, 185, 129)
                    box_bg = (236, 253, 245)
                    box_fg = (6, 95, 70)
                    pill_txt = "明天全天无课"
            else:
                if ongoing_c:
                    accent_col = (234, 88, 12)   # 鲜明珊瑚橙：正在上课
                    box_bg = (255, 247, 237)
                    box_fg = (154, 52, 18)
                    pill_txt = f"上课中 · 剩{format_duration_cn(ongoing_c.wait_or_remain_minutes)}"
                elif next_c:
                    accent_col = (37, 99, 235)   # 电光蓝：等待下一节
                    box_bg = (239, 246, 255)
                    box_fg = (30, 64, 175)
                    pill_txt = f"下一节 · {format_duration_cn(next_c.wait_or_remain_minutes)}后"
                else:
                    accent_col = (16, 185, 129)  # 翠绿：空闲可约
                    box_bg = (236, 253, 245)
                    box_fg = (6, 95, 70)
                    pill_txt = "空闲 · 随时可约"

            draw.rounded_rectangle(
                (pad_x, c_top, width - pad_x, c_bot),
                radius=26,
                fill=(255, 255, 255),
                outline=(204, 235, 232),
                width=2,
            )
            draw.rounded_rectangle(
                (pad_x, c_top + 16, pad_x + 12, c_bot - 16),
                radius=6,
                fill=accent_col,
            )

            # 左侧 96px 抗锯齿圆形 QQ 头像 + 状态圆点
            self._draw_avatar(
                img,
                av_path,
                dname,
                x=pad_x + 28,
                y=c_top + 24,
                size=92,
                ring_color=accent_col,
                ring_width=4,
                status_dot_color=accent_col,
            )

            # 第一行：群昵称 (32px Bold) + 右上角鲜明实心状态胶囊
            name_str = self._truncate_text(draw, dname, f_name, 260)
            self._draw_text(
                draw, (pad_x + 140, c_top + 24), name_str, (15, 23, 42), f_name, bold_stroke=True
            )

            pw, _ = self._text_size(draw, pill_txt, f_pill)
            px2 = width - pad_x - 24
            px1 = px2 - pw - 28
            draw.rounded_rectangle(
                (px1, c_top + 22, px2, c_top + 62),
                radius=20,
                fill=accent_col,
            )
            self._draw_text(
                draw, (px1 + 14, c_top + 29), pill_txt, (255, 255, 255), f_pill, bold_stroke=True
            )

            # 下方课程详情高对比度内嵌框
            ib_x1 = pad_x + 140
            ib_x2 = width - pad_x - 24
            ib_y1 = c_top + 78
            ib_y2 = c_bot - 20
            draw.rounded_rectangle(
                (ib_x1, ib_y1, ib_x2, ib_y2),
                radius=18,
                fill=box_bg,
            )
            inner_max_w = (ib_x2 - ib_x1) - 32

            if is_tomorrow:
                if not day_courses:
                    main_txt = "明日无任何课程安排"
                    sub_txt = "全天自由活动 · 可放心约饭或游戏"
                else:
                    first_c = day_courses[0]
                    loc = f" @{first_c.room}" if first_c.room else ""
                    main_txt = f"{first_c.start_time}-{first_c.end_time}  {first_c.course_name}{loc}"
                    if len(day_courses) > 1:
                        sec_c = day_courses[1]
                        more = f" 等{len(day_courses)}门课" if len(day_courses) > 2 else ""
                        sub_txt = f"后续：{sec_c.start_time} {sec_c.course_name}{more}"
                    else:
                        sub_txt = f"明天仅此 1 门课（共 {first_c.step} 节）"
            else:
                if ongoing_c:
                    loc = f" @{ongoing_c.room}" if ongoing_c.room else ""
                    main_txt = f"正在上：{ongoing_c.course_name}{loc}"
                    if next_c:
                        sub_txt = f"{ongoing_c.start_time}-{ongoing_c.end_time}  |  下一节 {next_c.start_time} {next_c.course_name}"
                    else:
                        sub_txt = f"{ongoing_c.start_time}-{ongoing_c.end_time}（上完这节今日就下课啦）"
                elif next_c:
                    loc = f" @{next_c.room}" if next_c.room else ""
                    main_txt = f"下一节：{next_c.course_name}{loc}"
                    sub_txt = f"上课时间 {next_c.start_time} - {next_c.end_time} ({next_c.node_range_str})"
                else:
                    if day_courses:
                        main_txt = f"今日 {len(day_courses)} 门课已全部上完"
                        sub_txt = f"最后下课时间 {day_courses[-1].end_time} · 当前完全空闲"
                    else:
                        main_txt = "今日全天无课"
                        sub_txt = "当前完全空闲 · 随时可以约饭或开黑"

            self._draw_text(
                draw,
                (ib_x1 + 16, ib_y1 + 16),
                self._truncate_text(draw, main_txt, f_main_line, inner_max_w),
                box_fg,
                f_main_line,
                bold_stroke=True,
            )
            self._draw_text(
                draw,
                (ib_x1 + 16, ib_y1 + 62),
                self._truncate_text(draw, sub_txt, f_sub_line, inner_max_w),
                (71, 85, 105),
                f_sub_line,
            )

            y_cursor += member_card_h + card_gap

        footer_str = f"AstrBot 课程表  ·  {now.strftime('%Y-%m-%d %H:%M')}"
        fw, _ = self._text_size(draw, footer_str, f_footer)
        self._draw_text(
            draw, ((width - fw) // 2, height - 44), footer_str, (148, 163, 184), f_footer
        )

        img.save(out_path, format="PNG")
        return out_path

    # =========================================================================
    # 样式三：本周上课排行榜（黑金荣耀榜，720px 竖版大字卡片）
    # =========================================================================
    def render_weekly_ranking(
        self,
        members_data: List[Tuple[UserProfile, ScheduleData, Optional[Path]]],
        group_id: Optional[str] = None,
        now: Optional[datetime] = None,
        tz_name: str = "Asia/Shanghai",
    ) -> Path:
        if now is None:
            now = now_in_tz(tz_name)
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
            total_mins = sum(c.duration_minutes for c in wk_courses)
            total_nodes = sum(c.step for c in wk_courses)
            early_count = sum(1 for c in wk_courses if c.start_time <= "08:30")

            ranking_rows.append(
                {
                    "profile": prof,
                    "display_name": prof.get_display_name(group_id),
                    "avatar_path": av_path,
                    "total_mins": total_mins,
                    "total_hours": round(total_mins / 60.0, 1),
                    "total_nodes": total_nodes,
                    "course_count": len(wk_courses),
                    "early_count": early_count,
                }
            )

        ranking_rows.sort(
            key=lambda x: (x["total_mins"], x["total_nodes"], x["course_count"]),
            reverse=True,
        )

        max_mins = max((r["total_mins"] for r in ranking_rows), default=1)
        if max_mins <= 0:
            max_mins = 1

        width = 720
        pad_x = 28
        card_w = width - pad_x * 2
        hero_h = 256
        row_h = 196
        row_gap = 20

        content_h = 28 + hero_h + 24 + len(ranking_rows) * (row_h + row_gap)
        height = max(860, content_h + 68)

        img = Image.new("RGB", (width, height), (246, 244, 240))
        draw = ImageDraw.Draw(img)

        f_hero_title = self._get_font(38, bold=True)
        f_hero_sub = self._get_font(22, bold=False)
        f_stat_val = self._get_font(36, bold=True)
        f_stat_lbl = self._get_font(20, bold=False)
        f_rank_num = self._get_font(28, bold=True)
        f_name = self._get_font(32, bold=True)
        f_tag = self._get_font(21, bold=True)
        f_hours = self._get_font(34, bold=True)
        f_nodes = self._get_font(24, bold=True)
        f_footer = self._get_font(20, bold=False)

        # 1. 顶部黑金荣耀 Hero 卡片
        hx1, hy1 = pad_x, 28
        hx2, hy2 = width - pad_x, 28 + hero_h
        draw.rounded_rectangle((hx1, hy1, hx2, hy2), radius=28, fill=(24, 24, 27))
        draw.rounded_rectangle(
            (hx1 + 28, hy1, hx1 + 170, hy1 + 8), radius=4, fill=(245, 158, 11)
        )

        self._draw_text(
            draw, (hx1 + 28, hy1 + 28), "本周群友上课排行榜", (255, 255, 255), f_hero_title, bold_stroke=True
        )
        sub_str = f"统计周期：{monday.strftime('%m月%d日')} - {sunday.strftime('%m月%d日')}（周一至周日）"
        self._draw_text(draw, (hx1 + 28, hy1 + 84), sub_str, (253, 230, 138), f_hero_sub)

        group_total_hours = round(sum(r["total_mins"] for r in ranking_rows) / 60.0, 1)
        group_total_nodes = sum(r["total_nodes"] for r in ranking_rows)
        stats = [
            ("上榜人数", f"{len(ranking_rows)} 人", (251, 191, 36)),
            ("全群总课时", f"{group_total_hours} 小时", (245, 158, 11)),
            ("全群总节数", f"{group_total_nodes} 节", (251, 146, 60)),
        ]
        bento_w = (card_w - 56 - 24) // 3
        for s_idx, (s_lbl, s_val, s_col) in enumerate(stats):
            bx = hx1 + 28 + s_idx * (bento_w + 12)
            by = hy1 + 138
            draw.rounded_rectangle(
                (bx, by, bx + bento_w, by + 94), radius=18, fill=(39, 39, 42)
            )
            self._draw_text(draw, (bx + 18, by + 14), s_lbl, (212, 212, 216), f_stat_lbl)
            self._draw_text(
                draw, (bx + 18, by + 44), s_val, s_col, f_stat_val, bold_stroke=True
            )

        y_cursor = hy2 + 24

        # 2. 排行榜成员卡片
        for idx, r in enumerate(ranking_rows):
            rank = idx + 1
            c_top = y_cursor
            c_bot = y_cursor + row_h

            if rank == 1:
                medal_col = (245, 158, 11)     # 冠军金
                tag_bg = (254, 243, 199)
                tag_fg = (180, 83, 9)
                rank_str = "NO.1"
                title_tag = "卷王之王" if r["total_mins"] > 0 else "清闲仙人"
            elif rank == 2:
                medal_col = (100, 116, 139)    # 亚军银
                tag_bg = (241, 245, 249)
                tag_fg = (51, 65, 85)
                rank_str = "NO.2"
                title_tag = "早八战神" if r["early_count"] >= 2 else "勤学标兵"
            elif rank == 3:
                medal_col = (217, 119, 6)      # 季军铜
                tag_bg = (255, 237, 213)
                tag_fg = (154, 52, 18)
                rank_str = "NO.3"
                title_tag = "勤学标兵" if r["total_mins"] >= 450 else "劳逸结合"
            else:
                medal_col = (148, 163, 184)
                tag_bg = (241, 245, 249)
                tag_fg = (100, 116, 139)
                rank_str = f"NO.{rank}"
                title_tag = "清闲仙人" if r["total_mins"] == 0 else "摸鱼行家"

            draw.rounded_rectangle(
                (pad_x, c_top, width - pad_x, c_bot),
                radius=26,
                fill=(255, 255, 255),
                outline=medal_col if rank <= 3 else (228, 228, 231),
                width=3 if rank == 1 else 2,
            )

            # 左侧头像 + 顶部排名角标
            self._draw_avatar(
                img,
                r["avatar_path"],
                r["display_name"],
                x=pad_x + 26,
                y=c_top + 24,
                size=96,
                ring_color=medal_col,
                ring_width=4,
            )

            # 头像下方排名胶囊
            rw, _ = self._text_size(draw, rank_str, f_rank_num)
            rx1 = pad_x + 26 + (96 - (rw + 28)) // 2
            ry1 = c_top + 132
            draw.rounded_rectangle(
                (rx1, ry1, rx1 + rw + 28, ry1 + 42),
                radius=14,
                fill=medal_col,
            )
            self._draw_text(
                draw, (rx1 + 14, ry1 + 6), rank_str, (255, 255, 255), f_rank_num, bold_stroke=True
            )

            # 右侧第一行：群昵称 (32px Bold) + 称号胶囊
            dname = self._truncate_text(draw, r["display_name"], f_name, 300)
            self._draw_text(
                draw, (pad_x + 148, c_top + 24), dname, (15, 23, 42), f_name, bold_stroke=True
            )

            tw, _ = self._text_size(draw, title_tag, f_tag)
            tx2 = width - pad_x - 24
            tx1 = tx2 - tw - 28
            draw.rounded_rectangle(
                (tx1, c_top + 24, tx2, c_top + 62),
                radius=19,
                fill=tag_bg,
            )
            self._draw_text(
                draw, (tx1 + 14, c_top + 31), title_tag, tag_fg, f_tag, bold_stroke=True
            )

            # 右侧第二行：大字时长与节数
            hours_str = f"{r['total_hours']} 小时"
            self._draw_text(
                draw, (pad_x + 148, c_top + 80), hours_str, medal_col if rank == 1 else (15, 23, 42), f_hours, bold_stroke=True
            )
            hw, _ = self._text_size(draw, hours_str, f_hours)

            nodes_str = f"共 {r['total_nodes']} 节 · {r['course_count']} 门课 · 早八 {r['early_count']} 次"
            nodes_str = self._truncate_text(draw, nodes_str, f_nodes, 480 - hw)
            self._draw_text(
                draw,
                (pad_x + 148 + hw + 18, c_top + 88),
                nodes_str,
                (100, 116, 139),
                f_nodes,
                bold_stroke=True,
            )

            # 右侧第三行：加粗鲜明进度条
            bar_x1 = pad_x + 148
            bar_x2 = width - pad_x - 24
            bar_y = c_top + 144
            draw.rounded_rectangle(
                (bar_x1, bar_y, bar_x2, bar_y + 22),
                radius=11,
                fill=(241, 245, 249),
            )
            ratio = min(1.0, max(0.06, r["total_mins"] / max_mins)) if r["total_mins"] > 0 else 0.04
            fill_w = max(24, int((bar_x2 - bar_x1) * ratio))
            draw.rounded_rectangle(
                (bar_x1, bar_y, bar_x1 + fill_w, bar_y + 22),
                radius=11,
                fill=medal_col if rank == 1 else ((37, 99, 235) if rank == 2 else (13, 148, 136)),
            )

            y_cursor += row_h + row_gap

        footer_str = f"AstrBot 课程表  ·  {now.strftime('%Y-%m-%d %H:%M')}"
        fw, _ = self._text_size(draw, footer_str, f_footer)
        self._draw_text(
            draw, ((width - fw) // 2, height - 44), footer_str, (148, 163, 184), f_footer
        )

        img.save(out_path, format="PNG")
        return out_path
