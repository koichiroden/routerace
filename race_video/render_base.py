# -*- coding: utf-8 -*-
"""
スタイリッシュな(箱根駅伝ルート紹介動画風の)ベースマップを1枚生成する。
どのレース設定(config)でも使えるように汎用化してある。
"""
import json
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .geo import compute_projection, project, CANVAS_W, CANVAS_H
from . import fonts as _fonts

FONT_BOLD, FONT_REGULAR, FONT_BLACK = _fonts.resolve()


def font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def load_all_lines(geojson_path):
    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for feat in data["features"]:
        geom = feat["geometry"]
        coords_list = [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]
        out.append((feat["properties"]["line_name"], coords_list))
    return out


def vertical_gradient(w, h, top_color, bottom_color):
    base = Image.new("RGB", (w, h), top_color)
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t = y / (h - 1)
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return base


def draw_glow_polyline(img, pts, color, width, glow_width, glow_alpha=90):
    if len(pts) < 2:
        return
    glow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.line(pts, fill=color + (glow_alpha,), width=glow_width, joint="curve")
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_width / 3))
    img.alpha_composite(glow_layer)

    line_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(line_layer)
    ld.line(pts, fill=color + (255,), width=width, joint="curve")
    for p in (pts[0], pts[-1]):
        r = width / 2
        ld.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color + (255,))
    img.alpha_composite(line_layer)


def render_base_map(config, paths, geojson_path="data/routes.geojson"):
    all_lines = load_all_lines(geojson_path)
    route_list = list(paths.values())

    focus_coords = []
    for r in route_list:
        focus_coords.extend(r["polyline"])
    proj = compute_projection(focus_coords, pad_ratio=0.16)

    bg = vertical_gradient(CANVAS_W, CANVAS_H, (8, 12, 28), (2, 4, 12))
    canvas = bg.convert("RGBA")

    # 背景テクスチャ: 全路線をうす暗いグレーで描画(表示範囲外は自然に切れる)
    faint = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    fd = ImageDraw.Draw(faint)
    for name, segs in all_lines:
        for seg in segs:
            pts = [project(proj, lon, lat) for lon, lat in seg]
            pts = [p for p in pts if -50 <= p[0] <= CANVAS_W + 50 and -50 <= p[1] <= CANVAS_H + 50]
            if len(pts) >= 2:
                fd.line(pts, fill=(120, 140, 190, 40), width=2, joint="curve")
    canvas.alpha_composite(faint)

    # 比較ルート(発光ライン)。後に描画した方が手前に見える。
    for route in reversed(route_list):
        color = tuple(route["color"])
        pts = [project(proj, lon, lat) for lon, lat in route["polyline"]]
        draw_glow_polyline(canvas, pts, color, width=10, glow_width=34, glow_alpha=90)

    draw = ImageDraw.Draw(canvas)
    f_station = font(FONT_BOLD, 26)

    def draw_station_dot(route, st, is_terminal):
        x, y = project(proj, st["lon"], st["lat"])
        r = 12 if is_terminal else 7
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 255),
                     outline=tuple(route["color"]) + (255,), width=4)
        if is_terminal:
            draw.ellipse([x - r - 6, y - r - 6, x + r + 6, y + r + 6],
                         outline=(255, 255, 255, 140), width=2)

    start_name = config.get("start_name", route_list[0]["stations"][0]["name"])
    end_label = config.get("end_label", route_list[0]["stations"][-1]["name"])

    for route in route_list:
        for st in route["stations"]:
            if st["popup"]:
                is_term = st["name"] in (start_name, end_label)
                draw_station_dot(route, st, is_term)

    start_pt = route_list[0]["stations"][0]
    x, y = project(proj, start_pt["lon"], start_pt["lat"])
    draw.text((x, y - 34), "START", font=font(FONT_BLACK, 24, index=0), fill=(255, 215, 0, 255), anchor="mm")
    draw.text((x, y - 62), start_name, font=f_station, fill=(255, 255, 255, 255), anchor="mm")

    finish_pts = [project(proj, r["stations"][-1]["lon"], r["stations"][-1]["lat"]) for r in route_list]
    fx = sum(p[0] for p in finish_pts) / len(finish_pts)
    fy = min(p[1] for p in finish_pts)
    draw.text((fx, fy + 42), "FINISH", font=font(FONT_BLACK, 24, index=0), fill=(255, 215, 0, 255), anchor="mm")
    draw.text((fx, fy + 70), end_label, font=f_station, fill=(255, 255, 255, 255), anchor="mm")

    # タイトル & 凡例
    f_title = font(FONT_BLACK, 60, index=0)
    f_sub = font(FONT_BOLD, 28)
    f_legend = font(FONT_BOLD, 28)

    draw.text((CANVAS_W / 2, 90), config.get("title_line1", ""), font=f_title,
               fill=(255, 255, 255, 255), anchor="mm")
    draw.text((CANVAS_W / 2, 148), config.get("title_line2", ""), font=f_sub,
               fill=(255, 215, 0, 255), anchor="mm")

    n = len(route_list)
    total_w = 0
    swatches = []
    tmp = Image.new("RGBA", (10, 10))
    tmpd = ImageDraw.Draw(tmp)
    for r in route_list:
        w = tmpd.textlength(r["name"], font=f_legend) + 46
        swatches.append(w)
        total_w += w + 40
    total_w -= 40
    cx = CANVAS_W / 2 - total_w / 2
    ly = 208
    for r, w in zip(route_list, swatches):
        draw.ellipse([cx, ly - 12, cx + 24, ly + 12], fill=tuple(r["color"]) + (255,))
        draw.text((cx + 34, ly), r["name"], font=f_legend, fill=(255, 255, 255, 255), anchor="lm")
        cx += w + 40

    return canvas, proj
