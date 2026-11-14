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

# =============================================================================
# 设计系统常量 ——「纸本 · 朱砂」
# =============================================================================
SS_MOBILE = 3  # 手机竖版图片内部超采样倍率
SS_WEEKLY = 2  # 周课表内部超采样倍率
OUT_SCALE = 1.5  # 最终输出倍率（逻辑 720px → 1080px 高清输出）

TL_AXIS = 128  # 时间轴竖线相对条目左侧的位置
TL_RIGHT = 158  # 时间轴条目右侧内容起点
META_LABEL_W = 52

PAPER = (250, 247, 241)  # 暖白纸底
INK = (28, 25, 23)  # 墨
INK_2 = (68, 63, 59)
INK_3 = (120, 113, 107)
INK_4 = (170, 163, 156)
RULE = (230, 224, 214)  # 发丝分隔线
RULE_DARK = (205, 197, 186)

ACCENT = (214, 69, 44)  # 朱砂
ACCENT_DARK = (150, 44, 24)
ACCENT_BG = (252, 236, 228)
INDIGO_INK = (44, 84, 150)  # 黛蓝
INDIGO_DARK = (28, 56, 104)
PINE = (38, 126, 98)  # 松绿
PINE_DARK = (22, 86, 66)
OCHRE = (160, 96, 52)  # 赭石

# 课程配色（主色 / 浅底 / 深字）：取自中国传统色，饱和而不刺眼，主色可承载白字
VIVID_PALETTES: List[Tuple[Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]]] = [
    ((214, 69, 44), (252, 234, 226), (140, 40, 22)),    # 朱砂
    ((44, 84, 150), (230, 236, 247), (26, 50, 96)),     # 黛蓝
    ((38, 126, 98), (225, 241, 234), (20, 80, 60)),     # 松绿
    ((190, 124, 24), (251, 241, 220), (115, 72, 8)),    # 姜黄
    ((112, 78, 168), (237, 232, 247), (66, 44, 108)),   # 青莲
    ((186, 58, 106), (249, 230, 238), (112, 28, 62)),   # 胭脂
    ((24, 122, 142), (223, 240, 244), (14, 76, 90)),    # 石青
    ((150, 92, 56), (244, 233, 224), (92, 54, 28)),     # 赭石
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
    # 设计风格「纸本 · 朱砂」
    #   暖白纸底 + 墨色排版 + 单一朱砂强调色 + 发丝细线分隔 + 大号数字排版
    #   单日课表采用时间轴布局；周课表采用实色课程色块；排行榜采用大号名次数字
    # =========================================================================

    # ---------------- 通用组件：刊头（Masthead） ----------------
    def _draw_masthead(
        self,
        cv: "_Canvas",
        P: float,
        y: float,
        kicker: str,
        kicker_right: str,
        title: str,
        avatar: Optional[Tuple[Optional[Path], str]] = None,
        right_block: Optional[Tuple[str, str, str]] = None,
    ) -> float:
        """绘制刊头：朱砂小标 + 大标题(可带头像) + 右侧大号数字块 + 双线；返回双线底部 y"""
        W = cv.w
        # 第一行：朱砂方块 + 字距小标 / 右侧辅助信息
        cv.rect((P, y + 3, P + 10, y + 13), 2, fill=ACCENT)
        kr_w = cv.tw(kicker_right, 16, bold=True) if kicker_right else 0
        avail_k = (W - P * 2) - 22 - (kr_w + 20 if kicker_right else 0)
        cv.spaced(P + 22, y + 8, kicker, 17, ACCENT, bold=True, max_w=avail_k)
        if kicker_right:
            cv.text(W - P, y + 8, kicker_right, 16, INK_3, bold=True, anchor="rm")

        # 第二行：头像 + 大标题 / 右侧大号数字块
        row_cy = y + 74
        right_left = W - P
        if right_block:
            big, small_top, small_bottom = right_block
            bw = cv.tw(big, 68, bold=True)
            cv.text(W - P, row_cy + 2, big, 68, INK, bold=True, anchor="rm")
            sx = W - P - bw - 12
            sw = max(cv.tw(small_top, 17, bold=True), cv.tw(small_bottom, 17))
            cv.text(sx, row_cy - 13, small_top, 17, ACCENT, bold=True, anchor="rm")
            cv.text(sx, row_cy + 15, small_bottom, 17, INK_3, anchor="rm")
            right_left = sx - sw - 20

        tx = P
        if avatar is not None:
            cv.avatar(avatar[0], avatar[1], P + 2, row_cy - 36, 72, ring=RULE, ring_w=2)
            tx = P + 94
        t_size = cv.fit(title, right_left - tx, 40, 20, bold=True)
        cv.text(tx, row_cy, title, t_size, INK, bold=True)

        # 双线
        ry = y + 130
        cv.rect((P, ry, W - P, ry + 3), 0, fill=INK)
        cv.rect((P, ry + 7, W - P, ry + 8), 0, fill=INK)
        return ry + 8

    # ---------------- 通用组件：数据栏 ----------------
    def _draw_stats_row(
        self,
        cv: "_Canvas",
        P: float,
        y: float,
        items: List[Tuple[str, str, Optional[Tuple[int, int, int]]]],
        value_col: Tuple[int, int, int] = INK,
    ) -> float:
        """三栏大号数字数据栏（标签 + 数字 + 单位，竖向细线分隔）；返回底部 y"""
        W = cv.w
        n = max(1, len(items))
        col_w = (W - P * 2) / n
        H = 100
        for i, (lbl, val, dot) in enumerate(items):
            x = P + i * col_w + (0 if i == 0 else 24)
            if i > 0:
                cv.rect((P + i * col_w, y + 20, P + i * col_w + 1, y + H - 14), 0, fill=RULE)
            lx = x
            if dot is not None:
                cv.ellipse((lx, y + 25, lx + 9, y + 34), fill=dot)
                lx += 16
            cv.text(lx, y + 30, lbl, 16, INK_3, bold=True)
            parts = val.split(" ", 1)
            num = parts[0]
            unit = parts[1] if len(parts) > 1 else ""
            avail = col_w - (0 if i == 0 else 24) - 8
            n_size = 44
            while n_size > 22 and cv.tw(num, n_size, True) + 8 + cv.tw(unit, 18, True) > avail:
                n_size -= 2
            cv.text(x, y + 70, num, n_size, value_col, bold=True)
            if unit:
                cv.text(x + cv.tw(num, n_size, True) + 6, y + 76, unit, 18, INK_3, bold=True)
        cv.rect((P, y + H, W - P, y + H + 1), 0, fill=RULE)
        return y + H + 1

    def _draw_footer(self, cv: "_Canvas", P: float, left: str, right: str) -> None:
        y = cv.h - 52
        cv.rect((P, y, cv.w - P, y + 1), 0, fill=RULE)
        cv.spaced(P, y + 26, left, 15, INK_4, bold=True, spacing=1.5)
        cv.text(cv.w - P, y + 26, right, 15, INK_4, anchor="rm")

    # ---------------- 课程条目配色（按上课状态） ----------------
    @staticmethod
    def _item_style(c: CourseInstance, idx: int) -> Dict[str, Any]:
        v_main, v_bg, v_dark = get_vivid_theme(c.course_name, idx)
        st: Dict[str, Any] = {"node_bg": v_bg, "node_fg": v_dark, "band": None}
        if c.status == "ongoing":
            st.update(
                dot=ACCENT, dot_style="halo", status_col=ACCENT,
                label=f"进行中 · 剩{format_duration_cn(c.wait_or_remain_minutes)}",
                title=INK, time=ACCENT, meta=INK_2, band=ACCENT_BG,
            )
        elif c.status == "next":
            st.update(
                dot=INK, dot_style="ring", status_col=INK,
                label=f"下一节 · {format_duration_cn(c.wait_or_remain_minutes)}后",
                title=INK, time=INK, meta=INK_2,
            )
        elif c.status == "finished":
            st.update(
                dot=INK_4, dot_style="solid", status_col=INK_4, label="已下课",
                title=INK_3, time=INK_4, meta=INK_4,
                node_bg=(240, 236, 229), node_fg=INK_3,
            )
        else:
            st.update(
                dot=v_main, dot_style="solid", status_col=v_main,
                label=f"{c.start_time} 开课", title=INK, time=INK, meta=INK_2,
            )
        return st

    # ---------------- 单日课程条目（时间轴） ----------------
    def _measure_course_card(self, m: Any, c: CourseInstance, w: int) -> Dict[str, Any]:
        """预计算时间轴条目的换行文本与高度，确保课程名/教室/教师/周数 100% 完整显示"""
        if not isinstance(m, _Canvas):
            m = _Canvas.measurer(self, draw=m, scale=1)
        inner_w = w - TL_RIGHT

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
            meta_rows.append((lbl, m.wrap(val, 20, val_w) or [val]))
        meta_lines = [ln for _, lines in meta_rows for ln in lines]

        right_h = 26 + 10 + len(course_lines) * t_lh + 8 + len(meta_lines) * 30
        if c.status == "ongoing":
            right_h += 20
        height = max(84, right_h) + 52
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
        """绘制时间轴条目（不含背景色带与轴线，由调用方统一绘制）"""
        if layout_info is None:
            layout_info = self._measure_course_card(cv, c, int(w))
        h = layout_info["height"]
        st = self._item_style(c, idx)
        top = y + 26

        # 左侧：开始 / 结束时间
        cv.text(x, top + 16, c.start_time, 30, st["time"], bold=True)
        cv.text(x, top + 50, c.end_time, 19, INK_3)

        # 轴线节点
        ax = x + TL_AXIS
        dy = top + 16
        bg_under = st["band"] or PAPER
        if st["dot_style"] == "halo":
            cv.ellipse((ax - 13, dy - 13, ax + 13, dy + 13), fill=_mix(ACCENT, bg_under, 0.72))
            cv.ellipse((ax - 7, dy - 7, ax + 7, dy + 7), fill=ACCENT)
        elif st["dot_style"] == "ring":
            cv.ellipse((ax - 8, dy - 8, ax + 8, dy + 8), fill=st["dot"])
            cv.ellipse((ax - 4.5, dy - 4.5, ax + 4.5, dy + 4.5), fill=bg_under)
        else:
            cv.ellipse((ax - 6, dy - 6, ax + 6, dy + 6), fill=st["dot"])

        # 右侧：状态 + 节次标签
        cx = x + TL_RIGHT
        right = x + w
        cv.text(cx, top + 13, st["label"], 17, st["status_col"], bold=True)
        cv.pill(right, top + 13, c.node_range_str, 15, st["node_fg"], st["node_bg"], pad=10, h=26, align="right")

        # 课程名
        ty = top + 26 + 10
        t_lh = layout_info["course_line_h"]
        for line in layout_info["course_lines"]:
            cv.text(cx, ty + t_lh / 2, line, layout_info["t_size"], st["title"], bold=True)
            ty += t_lh

        # 教室 / 教师 / 周数
        ty += 8
        for lbl, lines in layout_info["meta_rows"]:
            cv.text(cx, ty + 15, lbl, 16, INK_4, bold=True)
            for ln in lines:
                cv.text(cx + META_LABEL_W, ty + 15, ln, 20, st["meta"])
                ty += 30

        # 进行中：细进度条
        if c.status == "ongoing":
            by = ty + 10
            cv.rect((cx, by, right, by + 4), 2, fill=_mix(ACCENT, ACCENT_BG, 0.75))
            ratio = min(1.0, max(0.04, float(getattr(c, "progress", 0.0) or 0.0)))
            cv.rect((cx, by, cx + (right - cx) * ratio, by + 4), 2, fill=ACCENT)
        return h

    def _draw_timeline(
        self,
        cv: "_Canvas",
        courses: List[CourseInstance],
        layouts: List[Dict[str, Any]],
        x: float,
        y: float,
        w: float,
        idx_offset: int = 0,
    ) -> float:
        """绘制完整时间轴：背景色带 → 轴线 → 条目 → 分隔细线；返回底部 y"""
        positions: List[float] = []
        yy = y
        for l_info in layouts:
            positions.append(yy)
            yy += l_info["height"]
        if not positions:
            return y

        for c, py, l_info in zip(courses, positions, layouts):
            st = self._item_style(c, 0)
            if st["band"]:
                cv.rect((x - 16, py + 4, x + w + 16, py + l_info["height"] - 4), 14, fill=st["band"])

        ax = x + TL_AXIS
        first_dot = positions[0] + 42
        last_dot = positions[-1] + 42
        if last_dot > first_dot:
            cv.rect((ax - 1, first_dot, ax + 1, last_dot), 0, fill=RULE_DARK)

        for i, (c, py, l_info) in enumerate(zip(courses, positions, layouts)):
            self._draw_course_card(cv, c, i + idx_offset, x, py, w, l_info)
            if i < len(courses) - 1:
                nxt = courses[i + 1]
                if not (self._item_style(c, 0)["band"] or self._item_style(nxt, 0)["band"]):
                    by = py + l_info["height"]
                    cv.rect((x + TL_RIGHT, by, x + w, by + 1), 0, fill=RULE)
        return yy

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
        upcoming_courses = [c for c in all_courses if c.status in ("ongoing", "next", "upcoming")]
        show_courses = upcoming_courses if (only_upcoming and upcoming_courses) else all_courses
        show_tomorrow = (
            tomorrow_preview if (only_upcoming and not upcoming_courses and tomorrow_preview) else None
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
            out_path.write_bytes(create_minimal_png(720, 920, PAPER, INK, 160))
            return out_path

        W, P = 720, 40
        CW = W - P * 2

        m = _Canvas.measurer(self, scale=SS_MOBILE)
        today_layouts = [self._measure_course_card(m, c, CW) for c in show_courses]
        tomorrow_layouts = (
            [self._measure_course_card(m, tc, CW) for tc in show_tomorrow] if show_tomorrow else []
        )
        show_done = bool(only_upcoming and all_courses and not upcoming_courses)

        HEAD_H = 138 + 101  # 刊头 + 数据栏
        content_h = P + HEAD_H + 18
        if show_done:
            content_h += 56
        content_h += sum(l["height"] for l in today_layouts) if today_layouts else (220 if show_tomorrow else 300)
        if tomorrow_layouts:
            content_h += 70 + sum(l["height"] for l in tomorrow_layouts)
        H = max(560, content_h + 76)

        cv = _Canvas(self, W, H, bg=PAPER, scale=SS_MOBILE)

        week_label = f"第 {week_num}/{schedule.max_week} 周" if schedule.max_week > 0 else f"第 {week_num} 周"
        y = self._draw_masthead(
            cv, P, P, title_prefix, week_label, display_name,
            avatar=(avatar_path, display_name),
            right_block=(f"{target_date.day}", target_date.strftime("%m月").lstrip("0"), weekday_str),
        )
        y = self._draw_stats_row(
            cv, P, y,
            [
                ("明日课程" if "明日" in title_prefix else "今日课程", f"{len(all_courses)} 门", None),
                ("总计节次", f"{total_nodes} 节", None),
                ("课堂时长", f"{total_hours} 小时", None),
            ],
        )
        y += 18

        if show_done:
            msg = "今日课程已全部下课 · 以下为今日已上课程回顾"
            fs = cv.fit(msg, CW - 80, 18, 14, bold=True)
            mw = cv.tw(msg, fs, True)
            cy = y + 24
            cv.rect((P, cy, (W - mw) / 2 - 16, cy + 1), 0, fill=RULE_DARK)
            cv.rect(((W + mw) / 2 + 16, cy, W - P, cy + 1), 0, fill=RULE_DARK)
            cv.text(W / 2, cy, msg, fs, PINE, bold=True, anchor="mm")
            y += 56

        if show_courses:
            y = self._draw_timeline(cv, show_courses, today_layouts, P, y, CW)
        else:
            box_h = 220 if show_tomorrow else 300
            mid = W / 2
            icy = y + box_h * 0.36
            cv.ellipse((mid - 46, icy - 46, mid + 46, icy + 46), fill=RULE)
            cv.ellipse((mid - 44, icy - 44, mid + 44, icy + 44), fill=PAPER)
            cv.ellipse((mid - 30, icy - 30, mid + 30, icy + 30), fill=(244, 238, 228))
            cv.ellipse((mid - 9, icy - 9, mid + 9, icy + 9), fill=ACCENT)
            cv.text(mid, icy + 80, "当天无任何课程安排", 28, INK, bold=True, anchor="mm")
            cv.text(mid, icy + 118, "全天自由支配时间 · 尽情休息或约群友吧", 19, INK_3, anchor="mm")
            y += box_h

        if show_tomorrow and tomorrow_layouts:
            y += 20
            cv.rect((P, y, W - P, y + 2), 0, fill=INK)
            cv.rect((P, y + 18, P + 10, y + 28), 2, fill=ACCENT)
            cv.spaced(P + 22, y + 23, f"明日课程预览（共 {len(show_tomorrow)} 门课）", 18, INK, bold=True, spacing=2)
            y += 50
            y = self._draw_timeline(cv, show_tomorrow, tomorrow_layouts, P, y, CW, idx_offset=3)

        self._draw_footer(cv, P, "AstrBot 课程表", now.strftime("%Y-%m-%d %H:%M"))
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
            (20, 15, 26, 20),
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
            needed_h = len(name_lines) * name_lh + ((6 + len(meta_lines) * meta_lh) if meta_lines else 0)
            best = {
                "name_sz": name_sz, "meta_sz": meta_sz,
                "name_lines": name_lines, "meta_lines": meta_lines,
                "name_lh": name_lh, "meta_lh": meta_lh, "needed_h": needed_h,
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
            out_path.write_bytes(create_minimal_png(1060, 1180, PAPER, INK, 160))
            return out_path

        max_course_node = max((c.end_node for dlist in week_courses.values() for c in dlist), default=0)
        all_sched_node = max((c.start_node + max(1, c.step) - 1 for c in schedule.courses), default=0)
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
        W, P = 1080, 36
        COL_H = 74
        AXIS_W = 88
        col_w = (W - P * 2 - AXIS_W) / num_days
        INSET = 3
        PAD_X = 9
        inner_cell_w = col_w - INSET * 2 - PAD_X * 2

        m = _Canvas.measurer(self, scale=SS_WEEKLY)
        row_h = 94
        for day_list in week_courses.values():
            for c in day_list:
                span = max(1, c.end_node - c.start_node + 1)
                avail_h = span * row_h - INSET * 2 - 18
                plan = self._fit_weekly_cell_content(m, c, inner_cell_w, avail_h, _get_custom_time_tag(c))
                if plan["needed_h"] > avail_h:
                    req = int((plan["needed_h"] + INSET * 2 + 20) / span) + 2
                    if req > row_h:
                        row_h = min(140, req)

        all_wk = [c for dlist in week_courses.values() for c in dlist]
        total_wk_nodes = sum(c.step for c in all_wk)
        total_wk_hours = round(sum(c.duration_minutes for c in all_wk) / 60.0, 1)

        head_bottom = P + 138 + 101
        grid_top = head_bottom + 22
        body_top = grid_top + COL_H
        grid_bottom = body_top + max_nodes * row_h
        H = grid_bottom + 84

        cv = _Canvas(self, W, H, bg=PAPER, scale=SS_WEEKLY)

        range_str = f"{monday.strftime('%m/%d')} - {sunday.strftime('%m/%d')}（周一至周日）"
        y = self._draw_masthead(
            cv, P, P, title_prefix, range_str, display_name,
            avatar=(avatar_path, display_name),
            right_block=(f"{week_num}", "第", f"/ {schedule.max_week} 周" if schedule.max_week > 0 else "周"),
        )
        self._draw_stats_row(
            cv, P, y,
            [
                ("本周门次", f"{len(all_wk)} 门次", None),
                ("本周节数", f"{total_wk_nodes} 节", None),
                ("本周课时", f"{total_wk_hours} 小时", None),
            ],
        )

        today = now.date()
        today_idx = (today - monday).days if monday <= today <= sunday else -1
        gx1, gx2 = P, W - P

        # 今日列淡朱砂底
        if 0 <= today_idx < num_days:
            tx1 = gx1 + AXIS_W + today_idx * col_w
            cv.rect((tx1, grid_top, tx1 + col_w, grid_bottom), 0, fill=ACCENT_BG)

        # 表头
        cv.text(gx1 + AXIS_W / 2, grid_top + COL_H / 2, "节次", 17, INK_3, bold=True, anchor="mm")
        for d_idx in range(num_days):
            cur_d = monday + timedelta(days=d_idx)
            mid = gx1 + AXIS_W + d_idx * col_w + col_w / 2
            is_today = d_idx == today_idx
            wd_col = ACCENT if is_today else (INK if d_idx < 5 else INK_3)
            cv.text(mid, grid_top + 26, WEEKDAY_NAMES[d_idx], 20, wd_col, bold=True, anchor="mm")
            if is_today:
                cv.pill(mid, grid_top + 53, cur_d.strftime("%m/%d"), 14, (255, 255, 255), ACCENT, pad=9, h=22, align="center")
            else:
                cv.text(mid, grid_top + 53, cur_d.strftime("%m/%d"), 14, INK_3, anchor="mm")

        cv.rect((gx1, grid_top - 1, gx2, grid_top), 0, fill=RULE_DARK)
        cv.rect((gx1, body_top - 2, gx2, body_top), 0, fill=INK)

        # 节次轴 + 行线（大课间/午休/晚休 >= 25 分钟：朱砂虚线）
        for node in range(1, max_nodes + 1):
            ry1 = body_top + (node - 1) * row_h
            slot = slot_map.get(node)
            prev_slot = slot_map.get(node - 1) if node > 1 else None
            st_str = slot.start_time if slot else default_map.get(node, {}).get("start_time", "")
            et_str = slot.end_time if slot else default_map.get(node, {}).get("end_time", "")
            if node > 1:
                big = False
                if prev_slot and st_str and prev_slot.end_time:
                    if hhmm_to_minutes(st_str) - hhmm_to_minutes(prev_slot.end_time) >= 25:
                        big = True
                if big:
                    cv.dashed_hline(gx1, gx2, ry1, (226, 150, 128), 1.5, dash=7, gap=5)
                else:
                    cv.rect((gx1, ry1, gx2, ry1 + 1), 0, fill=RULE)
            ax = gx1 + AXIS_W / 2
            cv.text(ax, ry1 + 26, f"{node:02d}", 22, INK, bold=True, anchor="mm")
            if st_str:
                cv.text(ax, ry1 + 52, st_str, 13, INK_3, anchor="mm")
            if et_str:
                cv.text(ax, ry1 + 69, et_str, 13, INK_4, anchor="mm")

        for d_idx in range(0, num_days):
            cx = gx1 + AXIS_W + d_idx * col_w
            cv.rect((cx, grid_top + (0 if d_idx == 0 else 14), cx + 1, grid_bottom), 0, fill=RULE if d_idx else RULE_DARK)
        cv.rect((gx1, grid_bottom, gx2, grid_bottom + 2), 0, fill=INK)

        # 实色课程色块
        for wd, day_courses in week_courses.items():
            d_idx = wd - 1
            if not (0 <= d_idx < num_days):
                continue
            bx1 = gx1 + AXIS_W + d_idx * col_w + INSET + 0.5
            bx2 = bx1 + col_w - INSET * 2
            for idx, c in enumerate(day_courses):
                s_node = max(1, min(max_nodes, c.start_node))
                e_node = max(s_node, min(max_nodes, c.end_node))
                by1 = body_top + (s_node - 1) * row_h + INSET
                by2 = body_top + e_node * row_h - INSET
                v_main, _, _ = get_vivid_theme(c.course_name, idx)
                cv.rect((bx1, by1, bx2, by2), 8, fill=v_main)
                plan = self._fit_weekly_cell_content(cv, c, (bx2 - bx1) - PAD_X * 2, (by2 - by1) - 18, _get_custom_time_tag(c))
                tx = bx1 + PAD_X
                ty = by1 + 10
                for ln in plan["name_lines"]:
                    cv.text(tx, ty + plan["name_lh"] / 2, ln, plan["name_sz"], (255, 255, 255), bold=True)
                    ty += plan["name_lh"]
                if plan["meta_lines"]:
                    ty += 6
                    meta_col = _mix(v_main, (255, 255, 255), 0.84)
                    for ln in plan["meta_lines"]:
                        cv.text(tx, ty + plan["meta_lh"] / 2, ln, plan["meta_sz"], meta_col)
                        ty += plan["meta_lh"]

        self._draw_footer(cv, P, "AstrBot 课程表", f"第 {week_num} 周  ·  {now.strftime('%Y-%m-%d %H:%M')}")
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
            out_path.write_bytes(create_minimal_png(720, 920, PAPER, INK, 160))
            return out_path

        W, P = 720, 40
        AV = 60
        TX = P + AV + 22
        MAIN_SZ, MAIN_LH = 21, 31
        SUB_SZ, SUB_LH = 17, 26
        text_max_w = (W - P) - TX

        m = _Canvas.measurer(self, scale=SS_MOBILE)
        rows: List[Dict[str, Any]] = []
        ongoing_count = free_count = has_class_tomorrow_count = 0

        for prof, sched, av_path in members_data:
            day_courses = sched.get_courses_for_date(target_date, now=now, tz_name=tz_name)
            m_week = sched.get_week_number(target_date)
            ongoing_c = next((c for c in day_courses if c.status == "ongoing"), None)
            next_c = next((c for c in day_courses if c.status in ("next", "upcoming")), None)
            t_thresh = parse_hhmm(getattr(prof, "early_eight_threshold", "08:30") or "08:30")

            if is_tomorrow:
                if day_courses:
                    has_class_tomorrow_count += 1
                    sort_rank = (0, day_courses[0].start_time, -len(day_courses))
                    accent, fg = INDIGO_INK, INDIGO_DARK
                    total_nodes = sum(c.step for c in day_courses)
                    early_str = " · 早八" if parse_hhmm(day_courses[0].start_time) <= t_thresh else ""
                    pill_txt = f"明日 {len(day_courses)} 门 ({total_nodes}节){early_str}"
                    main_lines: List[str] = []
                    for c in day_courses:
                        loc = f" @{c.room}" if c.room else ""
                        main_lines.extend(m.wrap(f"{c.start_time}-{c.end_time} {c.course_name}{loc}", MAIN_SZ, text_max_w, bold=True))
                    sub_lines = m.wrap(
                        f"第{m_week}周 · 全天共 {total_nodes} 节 · {format_duration_cn(sum(c.duration_minutes for c in day_courses))}",
                        SUB_SZ, text_max_w,
                    )
                else:
                    free_count += 1
                    sort_rank = (1, "99:99", 0)
                    accent, fg = PINE, PINE_DARK
                    pill_txt = "明天全天无课"
                    main_lines = m.wrap("明日无任何课程安排", MAIN_SZ, text_max_w, bold=True)
                    sub_lines = m.wrap(f"第{m_week}周 · 全天自由活动 · 可放心约饭或游戏", SUB_SZ, text_max_w)
            else:
                if ongoing_c:
                    ongoing_count += 1
                    sort_rank = (0, ongoing_c.end_time, 0)
                    accent, fg = ACCENT, ACCENT_DARK
                    pill_txt = f"上课中 · 剩{format_duration_cn(ongoing_c.wait_or_remain_minutes)}"
                    loc = f" @{ongoing_c.room}" if ongoing_c.room else ""
                    main_raw = f"正在上：{ongoing_c.course_name}{loc}"
                    if next_c:
                        sub_raw = f"{ongoing_c.start_time}-{ongoing_c.end_time} | 下一节 {next_c.start_time} {next_c.course_name}"
                    else:
                        sub_raw = f"{ongoing_c.start_time}-{ongoing_c.end_time}（上完这节今日就下课啦）"
                elif next_c:
                    sort_rank = (1, next_c.start_time, 0)
                    accent, fg = INDIGO_INK, INDIGO_DARK
                    pill_txt = f"下一节 · {format_duration_cn(next_c.wait_or_remain_minutes)}后"
                    loc = f" @{next_c.room}" if next_c.room else ""
                    main_raw = f"下一节：{next_c.course_name}{loc}"
                    sub_raw = f"上课时间 {next_c.start_time} - {next_c.end_time} ({next_c.node_range_str})"
                else:
                    free_count += 1
                    sort_rank = (2, "99:99", 0)
                    accent, fg = PINE, PINE_DARK
                    pill_txt = "空闲 · 随时可约"
                    if day_courses:
                        main_raw = f"今日 {len(day_courses)} 门课已全部上完"
                        sub_raw = f"最后下课时间 {day_courses[-1].end_time} · 当前完全空闲"
                    else:
                        main_raw = "今日全天无课"
                        sub_raw = "当前完全空闲 · 随时可以约饭或开黑"
                main_lines = m.wrap(main_raw, MAIN_SZ, text_max_w, bold=True)
                sub_lines = m.wrap(sub_raw, SUB_SZ, text_max_w)

            body_h = 44 + len(main_lines) * MAIN_LH + 4 + len(sub_lines) * SUB_LH
            rows.append(
                {
                    "display_name": prof.get_display_name(group_id),
                    "avatar_path": av_path,
                    "accent": accent,
                    "fg": fg,
                    "pill_txt": pill_txt,
                    "main_lines": main_lines,
                    "sub_lines": sub_lines,
                    "h": max(AV, body_h) + 48,
                    "sort_rank": sort_rank,
                }
            )
        rows.sort(key=lambda r: r["sort_rank"])

        content_h = P + 138 + 101 + 8 + (sum(r["h"] for r in rows) if rows else 180)
        H = max(560, content_h + 76)
        cv = _Canvas(self, W, H, bg=PAPER, scale=SS_MOBILE)

        title_str = "群友明日课表总览" if is_tomorrow else "群友实时上课状态"
        y = self._draw_masthead(
            cv, P, P,
            f"{target_date.strftime('%Y年%m月%d日')} {weekday_str}",
            f"查询时间 {now.strftime('%H:%M')}",
            title_str,
            right_block=(f"{len(rows)}", "群友", "已绑定"),
        )
        if is_tomorrow:
            stats = [
                ("已绑群友", f"{len(rows)} 人", INK),
                ("明日有课", f"{has_class_tomorrow_count} 人", INDIGO_INK),
                ("明日无课", f"{free_count} 人", PINE),
            ]
        else:
            stats = [
                ("已绑群友", f"{len(rows)} 人", INK),
                ("正在上课", f"{ongoing_count} 人", ACCENT),
                ("空闲可约", f"{free_count} 人", PINE),
            ]
        y = self._draw_stats_row(cv, P, y, stats) + 8

        for i, r in enumerate(rows):
            top = y
            accent = r["accent"]
            cv.avatar(r["avatar_path"], r["display_name"], P, top + 24, AV, ring=None, ring_w=0, dot=accent)

            px1, _ = cv.pill(
                W - P, top + 42, r["pill_txt"], cv.fit(r["pill_txt"], 250, 16, 13, bold=True),
                r["fg"], _mix(accent, PAPER, 0.86), pad=12, h=30, align="right",
            )
            cv.text(TX, top + 42, r["display_name"], cv.fit(r["display_name"], max(120, px1 - TX - 12), 25, 16, bold=True), INK, bold=True)

            ty = top + 24 + 44
            for ln in r["main_lines"]:
                cv.text(TX, ty + MAIN_LH / 2, ln, MAIN_SZ, r["fg"], bold=True)
                ty += MAIN_LH
            ty += 4
            for ln in r["sub_lines"]:
                cv.text(TX, ty + SUB_LH / 2, ln, SUB_SZ, INK_3)
                ty += SUB_LH
            y += r["h"]
            if i < len(rows) - 1:
                cv.rect((TX, y, W - P, y + 1), 0, fill=RULE)

        if not rows:
            cv.text(W / 2, y + 90, "暂无已绑定课表的群友", 22, INK_3, bold=True, anchor="mm")

        self._draw_footer(cv, P, "AstrBot 课程表", now.strftime("%Y-%m-%d %H:%M"))
        cv.save(out_path)
        return out_path

    # =========================================================================
    # 样式三：本周/下周上课排行榜
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
            out_path.write_bytes(create_minimal_png(720, 920, PAPER, INK, 160))
            return out_path

        ranking_rows: List[Dict[str, Any]] = []
        for prof, sched, av_path in members_data:
            wk_map = sched.get_courses_for_week(monday, now=now, tz_name=tz_name)
            wk_courses = [c for dlist in wk_map.values() for c in dlist]
            total_mins = sum(c.duration_minutes for c in wk_courses)
            t_thresh = parse_hhmm(getattr(prof, "early_eight_threshold", "08:30") or "08:30")
            ranking_rows.append(
                {
                    "display_name": prof.get_display_name(group_id),
                    "avatar_path": av_path,
                    "week_num": sched.get_week_number(monday),
                    "total_mins": total_mins,
                    "total_hours": round(total_mins / 60.0, 1),
                    "total_nodes": sum(c.step for c in wk_courses),
                    "course_count": len(wk_courses),
                    "early_count": sum(1 for c in wk_courses if parse_hhmm(c.start_time) <= t_thresh),
                }
            )
        ranking_rows.sort(key=lambda x: (x["total_mins"], x["total_nodes"], x["course_count"]), reverse=True)
        max_mins = max((r["total_mins"] for r in ranking_rows), default=1) or 1

        W, P = 720, 40
        RANK_W = 70
        AV = 56
        CX = P + RANK_W + AV + 20
        content_w = (W - P) - CX

        m = _Canvas.measurer(self, scale=SS_MOBILE)
        for r in ranking_rows:
            s = f"第{r['week_num']}周 · 共 {r['total_nodes']} 节 · {r['course_count']} 门课 · 早八 {r['early_count']} 次"
            r["stats_size"] = m.fit(s, content_w, 18, 14)
            r["stats_lines"] = m.wrap(s, r["stats_size"], content_w)
            r["h"] = 24 + 36 + 50 + len(r["stats_lines"]) * 26 + 14 + 6 + 26

        content_h = P + 138 + 101 + 10 + (sum(r["h"] for r in ranking_rows) if ranking_rows else 180)
        H = max(560, content_h + 76)
        cv = _Canvas(self, W, H, bg=PAPER, scale=SS_MOBILE)

        y = self._draw_masthead(
            cv, P, P,
            f"统计周期：{monday.strftime('%m月%d日')} - {sunday.strftime('%m月%d日')}（周一至周日）",
            "",
            title_text,
        )
        y = self._draw_stats_row(
            cv, P, y,
            [
                ("上榜人数", f"{len(ranking_rows)} 人", None),
                ("全群总课时", f"{round(sum(r['total_mins'] for r in ranking_rows) / 60.0, 1)} 小时", None),
                ("全群总节数", f"{sum(r['total_nodes'] for r in ranking_rows)} 节", None),
            ],
        ) + 10

        for idx, r in enumerate(ranking_rows):
            rank = idx + 1
            if rank == 1:
                rank_col, bar_col = ACCENT, ACCENT
                tag_bg, tag_fg = (250, 224, 214), ACCENT_DARK
                title_tag = "卷王之王" if r["total_mins"] > 0 else "清闲仙人"
            elif rank == 2:
                rank_col, bar_col = INK, INK
                tag_bg, tag_fg = (230, 234, 242), INDIGO_DARK
                title_tag = "早八战神" if r["early_count"] >= 2 else "勤学标兵"
            elif rank == 3:
                rank_col, bar_col = OCHRE, OCHRE
                tag_bg, tag_fg = (243, 230, 218), (120, 70, 36)
                title_tag = "勤学标兵" if r["total_mins"] >= 450 else "劳逸结合"
            else:
                rank_col, bar_col = INK_4, INK_4
                tag_bg, tag_fg = (238, 234, 227), INK_3
                title_tag = "清闲仙人" if r["total_mins"] == 0 else "摸鱼行家"

            top = y
            h = r["h"]
            if rank == 1:
                cv.rect((P - 16, top + 6, W - P + 16, top + h - 6), 14, fill=ACCENT_BG)

            # 名次：NO. + 大号数字
            cv.spaced(P, top + 34, "NO.", 13, rank_col if rank <= 3 else INK_4, bold=True, spacing=2)
            cv.text(P - 2, top + 72, f"{rank}", 46 if rank < 10 else 36, rank_col, bold=True)

            cv.avatar(r["avatar_path"], r["display_name"], P + RANK_W, top + 26, AV, ring=rank_col if rank <= 3 else RULE, ring_w=2)

            tag_x1, _ = cv.pill(W - P, top + 42, title_tag, 15, tag_fg, tag_bg, pad=11, h=28, align="right")
            cv.text(CX, top + 42, r["display_name"], cv.fit(r["display_name"], max(120, tag_x1 - CX - 12), 24, 16, bold=True), INK, bold=True)

            hv = f"{r['total_hours']}"
            cv.text(CX, top + 88, hv, 38, ACCENT if rank == 1 else INK, bold=True)
            cv.text(CX + cv.tw(hv, 38, True) + 6, top + 95, "小时", 17, INK_3, bold=True)

            ty = top + 112
            for ln in r["stats_lines"]:
                cv.text(CX, ty + 13, ln, r["stats_size"], INK_3)
                ty += 26
            by = ty + 14
            cv.rect((CX, by, W - P, by + 6), 3, fill=(234, 228, 219))
            ratio = min(1.0, max(0.06, r["total_mins"] / max_mins)) if r["total_mins"] > 0 else 0.03
            cv.rect((CX, by, CX + max(8, (W - P - CX) * ratio), by + 6), 3, fill=bar_col)

            y += h
            if idx < len(ranking_rows) - 1 and rank != 1:
                cv.rect((P, y, W - P, y + 1), 0, fill=RULE)

        if not ranking_rows:
            cv.text(W / 2, y + 90, "暂无已绑定课表的群友", 22, INK_3, bold=True, anchor="mm")

        self._draw_footer(cv, P, "AstrBot 课程表", now.strftime("%Y-%m-%d %H:%M"))
        cv.save(out_path)
        return out_path


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
        bg: Tuple[int, int, int] = PAPER,
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

    def spaced(
        self,
        x: float,
        y: float,
        text: str,
        size: float,
        color: Tuple[int, int, int],
        bold: bool = False,
        spacing: float = 3,
        max_w: Optional[float] = None,
    ) -> float:
        """带字距的小标题文字（左对齐、垂直居中），超宽时自动收紧字距与字号；返回结束 x"""
        clean = strip_unsupported_chars(text)
        if not clean:
            return x
        sp, sz = spacing, size
        if max_w is not None:
            while sz > 12 and self.tw(clean, sz, bold) + sp * (len(clean) - 1) > max_w:
                if sp > 0:
                    sp = max(0.0, sp - 1)
                else:
                    sz -= 1
        cx = x
        for ch in clean:
            self.text(cx, y, ch, sz, color, bold=bold)
            cx += self.tw(ch, sz, bold) + sp if ch.strip() else sz * 0.35 + sp
        return cx

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
