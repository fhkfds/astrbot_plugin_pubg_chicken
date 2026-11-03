"""Render a PUBG squad victory report from match data (Pillow)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import os

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class Player:
    name: str
    kills: int
    damage: float
    assists: int
    knocks: int
    headshots: int = 0
    revives: int = 0
    longest_kill: float = 0
    monitored: bool = False


@dataclass(frozen=True)
class Victory:
    players: tuple[Player, ...]
    map_name: str
    mode: str
    match_time: str
    duration: int
    match_id: str = ""
    sample: bool = False


def _font_path(bold: bool, override: str | None) -> str:
    candidates = [
        override,
        os.getenv("PUBG_CARD_FONT"),
        str(Path(__file__).parent / "assets" / "NotoSansSC.ttf"),
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    raise RuntimeError("未找到中文字体，请设置 PUBG_CARD_FONT 或安装 Noto CJK 字体。")


def render_victory(report: Victory, output: str | Path, font_path: str | None = None) -> Path:
    if not report.players:
        raise ValueError("战报至少需要一名队员")
    width, row_h = 1200, 158
    height = 685 + len(report.players) * row_h
    img = Image.new("RGB", (width, height), "#0b1016")
    pix = img.load()
    # A restrained navy gradient, with an amber glow behind the victory rank.
    for y in range(height):
        for x in range(width):
            glow = max(0, 1 - math.hypot((x - 1070) / 750, (y - 135) / 500))
            pix[x, y] = (int(11 + 24 * glow), int(16 + 17 * glow), int(22 + 5 * glow))
    draw = ImageDraw.Draw(img)
    fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}

    def font(size, bold=False):
        key = size, bold
        if key not in fonts:
            fonts[key] = ImageFont.truetype(_font_path(bold, font_path), size)
            # The bundled Noto Sans SC is a variable font; select its weight.
            try:
                axes = fonts[key].get_variation_axes()
                values = [700 if axis["name"] == b"Weight" and bold
                          else 400 if axis["name"] == b"Weight"
                          else axis["default"] for axis in axes]
                fonts[key].set_variation_by_axes(values)
            except (OSError, AttributeError):
                pass  # System fonts can be static faces.
        return fonts[key]

    def text(x, y, value, size=24, color="#e9edf2", bold=False, anchor=None):
        draw.text((x, y), str(value), font=font(size, bold), fill=color, anchor=anchor)

    def fit(value, size, max_width, bold=False):
        if draw.textlength(value, font=font(size, bold)) <= max_width:
            return value
        while value and draw.textlength(value + "…", font=font(size, bold)) > max_width:
            value = value[:-1]
        return value + "…"

    def box(bounds, fill, radius=16, outline=None):
        draw.rounded_rectangle(bounds, radius=radius, fill=fill, outline=outline, width=1)

    gold, muted, dim = "#ffc75a", "#8995a4", "#586474"
    # Header ornaments are code drawn, so the live report needs no remote assets.
    for offset in range(5):
        x = 750 + offset * 100
        draw.line([(x, 0), (x - 125, 255), (x + 50, 400)], fill="#2a2b25", width=1)
    draw.line((54, 38, 54, 80), fill=gold, width=5)
    text(74, 41, "PUBG", 28, gold, True)
    text(172, 47, "/  SQUAD VICTORY", 19, muted)
    if report.sample:
        box((1016, 43, 1145, 79), "#2b2b24", 7, "#5b4a2c")
        text(1080, 50, "样式预览", 18, gold, anchor="mt")

    text(53, 126, "大吉大利", 68, "#f6f4ed", True)
    text(53, 210, "今晚吃鸡", 68, gold, True)
    text(58, 307, "WINNER WINNER CHICKEN DINNER", 19, "#b0a48c")
    text(1110, 94, "#1", 147, gold, True, "rt")
    text(1104, 281, "全场第一  /  VICTORY", 18, "#c3b08a", anchor="rt")

    chip_x = 58
    for label in [report.map_name, report.mode, "STEAM"]:
        chip_w = int(draw.textlength(label, font=font(19))) + 32
        box((chip_x, 359, chip_x + chip_w, 397), "#1c242c", 7, "#303943")
        text(chip_x + 16, 366, label, 19, "#b8c2ce")
        chip_x += chip_w + 12
    text(1140, 367, report.match_time, 20, muted, anchor="rt")

    # Three compact team summaries.
    totals = [
        ("团队击杀", str(sum(p.kills for p in report.players)), "KILLS"),
        ("团队伤害", f"{sum(p.damage for p in report.players):,.0f}", "DAMAGE"),
        ("对局时长", f"{report.duration // 60:02d}:{report.duration % 60:02d}", "DURATION"),
    ]
    for i, (label, value, unit) in enumerate(totals):
        x = 58 + i * 367
        box((x, 428, x + 350, 548), "#151d26", 12, "#26313c")
        text(x + 24, 445, label, 20, muted)
        text(x + 23, 477, value, 39, "#f4f6f8", True)
        text(x + 326, 497, unit, 13, dim, anchor="rt")

    text(58, 579, "冠军小队", 27, "#eef1f5", True)
    text(202, 587, f"{len(report.players):02d}  PLAYERS", 15, dim)
    cols = [(596, "击杀"), (765, "伤害"), (943, "助攻"), (1080, "击倒")]
    for x, label in cols:
        text(x, 587, label, 20, muted, anchor="mt")

    max_damage = max(max(p.damage for p in report.players), 1)
    mvp = max(range(len(report.players)), key=lambda i: report.players[i].damage)
    for i, p in enumerate(report.players):
        y = 631 + i * row_h
        box((58, y, 1142, y + 143), "#151e28" if i == mvp else "#111922", 12,
            "#51432d" if i == mvp else "#25303b")
        if i == mvp:
            draw.rounded_rectangle((58, y + 18, 62, y + 125), 2, fill=gold)
        text(82, y + 26, f"0{i + 1}", 19, dim)
        name = fit(p.name, 27, 350, True)
        text(128, y + 21, name, 27, "#f1f4f8", True)
        badge_x = 128
        for label, fill, color in (
            [("监控账号", "#1d3533", "#80cdb7")] if p.monitored else []
        ) + ([("伤害最高", "#3b3020", gold)] if i == mvp else []):
            bw = int(draw.textlength(label, font=font(15))) + 20
            box((badge_x, y + 65, badge_x + bw, y + 94), fill, 5)
            text(badge_x + 10, y + 69, label, 15, color)
            badge_x += bw + 8
        text(128, y + 108,
             f"爆头 {p.headshots}   ·   救援 {p.revives}   ·   最远击杀 {p.longest_kill:.0f}m",
             16, muted)
        for x, value, color in [
            (596, p.kills, "#f6f4ee"), (765, f"{p.damage:,.0f}", gold),
            (943, p.assists, "#d7e0e8"), (1080, p.knocks, "#d7e0e8"),
        ]:
            text(x, y + 31, value, 38, color, True, "mt")
        # Damage bars use a shared scale across the squad.
        draw.rounded_rectangle((697, y + 94, 833, y + 99), 2, fill="#29313a")
        bar = max(4, int(136 * p.damage / max_damage))
        draw.rounded_rectangle((697, y + 94, 697 + bar, y + 99), 2,
                               fill=gold if i == mvp else "#786844")

    footer_y = 631 + len(report.players) * row_h + 23
    text(58, footer_y, "PUBG 战绩订阅", 17, muted)
    text(1142, footer_y,
         "示例数据 · 非真实战绩" if report.sample else "数据来自 PUBG 官方 API · 北京时间",
         16, dim, anchor="rt")
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


def sample_report() -> Victory:
    return Victory(
        players=(
            Player("NightHunter", 12, 1568, 4, 9, 5, 2, 286, True),
            Player("CloudRunner", 6, 842, 6, 5, 2, 1, 174, True),
            Player("LuckyPotato", 4, 563, 3, 4, 1, 3, 132),
            Player("JustOneMore", 2, 311, 5, 2, 1, 2, 87),
        ),
        map_name="艾伦格 / ERANGEL", mode="四排 · FPP",
        match_time="2026.11.03  21:38", duration=1902,
        match_id="preview-only", sample=True,
    )


if __name__ == "__main__":
    result = render_victory(sample_report(), Path(__file__).parent / "previews" / "victory-card.png")
    print(result)
