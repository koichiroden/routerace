# -*- coding: utf-8 -*-
"""
電車最速バトル動画レンダラー(汎用版)。
ベースマップの上に、毎フレーム: 進行済みルートのハイライト, 非回転の車両
アイコン, 通過駅ポップアップ, 実況テロップ, スコアボードを描画する。
フレームをPNG連番で書き出し、最後にffmpegでmp4にエンコードする。
"""
import os
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .geo import project, CANVAS_W, CANVAS_H
from .motion import RouteMotion
from .commentary import build_events, write_script_files
from . import fonts as _fonts

FONT_BOLD, _FONT_REGULAR, FONT_BLACK = _fonts.resolve()

FPS = 30
OUTRO_HOLD_SEC = 4.0


def font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def ease_out_back(x):
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def load_icon(route_cfg, size=54):
    """config で icon_path (透過PNG, 進行方向=右向き推奨)が指定されていれば読み込む。
    無ければ簡易な非回転プレースホルダーアイコンを描く。どちらも回転はさせない
    (要件: 車両アイコンは回転しない)。"""
    path = route_cfg.get("icon_path")
    if path and os.path.exists(path):
        img = Image.open(path).convert("RGBA")
        w = size * 1.6
        h = w * img.height / img.width
        return img.resize((int(w), int(h)))
    return None  # None なら animate.py 側で描画プレースホルダーを使う


def draw_train_icon(canvas_rgba, cx, cy, color, icon_img=None, size=54):
    if icon_img is not None:
        w, h = icon_img.size
        canvas_rgba.alpha_composite(icon_img, (int(cx - w / 2), int(cy - h / 2)))
        return

    w, h = size, int(size * 0.62)
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0 = cx - w / 2, cy - h / 2
    x1, y1 = cx + w / 2, cy + h / 2
    d.rounded_rectangle([x0 + 3, y0 + 6, x1 + 3, y1 + 6], radius=h / 2, fill=(0, 0, 0, 90))
    d.rounded_rectangle([x0, y0, x1, y1], radius=h / 2, fill=color + (255,), outline=(255, 255, 255, 230), width=3)
    win_w = w * 0.16
    gap = w * 0.06
    start = x0 + w * 0.16
    for i in range(3):
        wx0 = start + i * (win_w + gap)
        d.rounded_rectangle([wx0, y0 + h * 0.22, wx0 + win_w, y0 + h * 0.6], radius=3,
                             fill=(235, 245, 255, 255))
    d.ellipse([x1 - h * 0.22, y0 + h * 0.62, x1 - h * 0.02, y0 + h * 0.9], fill=(255, 240, 150, 255))
    canvas_rgba.alpha_composite(layer)


def draw_progress_route(canvas_rgba, proj, route, motion, real_min, color):
    km_now = motion.km_at(real_min)
    cd = route["cum_dist"]
    poly = route["polyline"]
    pts = []
    for i, d in enumerate(cd):
        if d > km_now:
            break
        pts.append(project(proj, poly[i][0], poly[i][1]))
    lon, lat = motion.lonlat_at(real_min)
    pts.append(project(proj, lon, lat))
    if len(pts) >= 2:
        # GaussianBlurはピクセル数に比例して重いので、キャンバス全体(1080x1920)
        # ではなく、線の外接矩形+余白だけを切り出してぼかす(見た目は同じまま、
        # CPUの弱い環境でも1フレームあたりのコストを大きく減らせる)。
        cw, ch = canvas_rgba.size
        pad = 60  # ぼかし半径8 + 線幅22 を考慮した余裕
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bx0 = max(0, int(min(xs) - pad))
        by0 = max(0, int(min(ys) - pad))
        bx1 = min(cw, int(max(xs) + pad))
        by1 = min(ch, int(max(ys) + pad))
        if bx1 <= bx0 or by1 <= by0:
            return
        local_pts = [(x - bx0, y - by0) for x, y in pts]

        crop = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
        gd = ImageDraw.Draw(crop)
        gd.line(local_pts, fill=color + (140,), width=22, joint="curve")
        crop = crop.filter(ImageFilter.GaussianBlur(8))
        canvas_rgba.alpha_composite(crop, (bx0, by0))

        crop2 = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
        ld = ImageDraw.Draw(crop2)
        ld.line(local_pts, fill=(255, 255, 255, 235), width=6, joint="curve")
        canvas_rgba.alpha_composite(crop2, (bx0, by0))


def draw_popup(canvas_rgba, proj, station, elapsed, color):
    x, y = project(proj, station["lon"], station["lat"])
    if elapsed < 0.18:
        t = elapsed / 0.18
        scale = 0.3 + 1.9 * ease_out_back(t)
        alpha = int(255 * min(1.0, t * 1.3))
    elif elapsed < 0.42:
        t = (elapsed - 0.18) / 0.24
        scale = 2.2 - 1.2 * t
        alpha = 255
    elif elapsed < 1.25:
        scale = 1.0
        alpha = 255
    elif elapsed < 1.6:
        t = (elapsed - 1.25) / 0.35
        scale = 1.0
        alpha = int(255 * (1 - t))
    else:
        return

    f = font(FONT_BLACK, int(34 * scale), index=0)
    text = station["name"]
    bbox = f.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 22 * scale, 12 * scale
    bx0, by0 = x - tw / 2 - pad_x, y - 70 * scale - th - pad_y
    bx1, by1 = x + tw / 2 + pad_x, y - 70 * scale + pad_y

    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle([bx0, by0, bx1, by1], radius=14 * scale,
                          fill=(15, 18, 30, int(230 * (alpha / 255))),
                          outline=color + (alpha,), width=3)
    ld.polygon([(x - 10 * scale, by1), (x + 10 * scale, by1), (x, by1 + 16 * scale)],
               fill=(15, 18, 30, int(230 * (alpha / 255))))
    ld.text((x, (by0 + by1) / 2), text, font=f, fill=(255, 255, 255, alpha), anchor="mm")
    canvas_rgba.alpha_composite(layer)


def draw_caption(canvas_rgba, text, speaker, color, alpha=255):
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    y0, y1 = 1690, 1770
    d.rectangle([0, y0, CANVAS_W, y1], fill=(0, 0, 0, int(150 * alpha / 255)))
    f_tag = font(FONT_BOLD, 24)
    f_text = font(FONT_BOLD, 30)
    tag_w = max(60, int(len(speaker) * 15 + 30))
    d.rounded_rectangle([26, y0 + 12, 26 + tag_w, y0 + 44], radius=10, fill=tuple(color) + (alpha,))
    d.text((26 + tag_w / 2, y0 + 28), speaker, font=f_tag, fill=(10, 10, 15, alpha), anchor="mm")
    max_chars = 22
    lines = [text[i:i + max_chars] for i in range(0, len(text), max_chars)][:2]
    ty = y0 + 60
    for ln in lines:
        d.text((CANVAS_W / 2, ty), ln, font=f_text, fill=(255, 255, 255, alpha), anchor="mm")
        ty += 34
    canvas_rgba.alpha_composite(layer)


def draw_scoreboard(canvas_rgba, route_list, motions, real_mins):
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f_name = font(FONT_BOLD, 30)
    f_time = font(FONT_BLACK, 30, index=0)
    bar_x0, bar_x1 = 230, 820
    row_ys = [1810, 1880] if len(route_list) <= 2 else \
        [1780 + i * 50 for i in range(len(route_list))]
    for route, y in zip(route_list, row_ys):
        color = tuple(route["color"])
        m = motions[route["key"]]
        rmin = real_mins[route["key"]]
        frac = m.progress(rmin)
        d.text((40, y), route["short_name"], font=f_name, fill=(255, 255, 255, 255), anchor="lm")
        d.rounded_rectangle([bar_x0, y - 10, bar_x1, y + 10], radius=10, fill=(255, 255, 255, 40))
        fill_x = bar_x0 + (bar_x1 - bar_x0) * frac
        if fill_x > bar_x0:
            d.rounded_rectangle([bar_x0, y - 10, fill_x, y + 10], radius=10, fill=color + (255,))
        label = f"{min(rmin, route['total_min']):.0f}分" + (" GOAL" if m.finished(rmin) else "")
        d.text((1040, y), label, font=f_time, fill=(255, 255, 255, 255), anchor="rm")
    canvas_rgba.alpha_composite(layer)


def draw_result_panel(canvas_rgba, config, route_list, alpha):
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(0, 0, 0, int(140 * alpha / 255)))
    cx, cy = CANVAS_W / 2, CANVAS_H / 2 - 60

    winner = min(route_list, key=lambda r: r["total_min"])
    loser_times = [r["total_min"] for r in route_list if r is not winner]
    diff = min(loser_times, default=winner["total_min"]) - winner["total_min"]

    f_big = font(FONT_BLACK, 76, index=0)
    f_mid = font(FONT_BOLD, 40)
    f_small = font(FONT_BOLD, 32)

    d.text((cx, cy - 180), "RESULT", font=f_big, fill=(255, 215, 0, alpha), anchor="mm")
    d.text((cx, cy - 90), f"{winner['name']} の勝ち!", font=f_mid, fill=(255, 255, 255, alpha), anchor="mm")
    d.text((cx, cy - 40), f"( 差 {diff:.0f} 分 )", font=f_mid, fill=tuple(winner["color"]) + (alpha,), anchor="mm")

    n = len(route_list)
    xs = [cx + (i - (n - 1) / 2) * 300 for i in range(n)]
    for r, x in zip(route_list, xs):
        d.text((x, cy + 60), r["short_name"], font=f_small, fill=tuple(r["color"]) + (alpha,), anchor="mm")
        d.text((x, cy + 105), f"{r['total_min']}分", font=f_mid, fill=(255, 255, 255, alpha), anchor="mm")

    canvas_rgba.alpha_composite(layer)


def render(config, paths, base_map_rgba, proj, out_dir="output", frames_dir="frames",
           fps=FPS, fast_preview=False, out_name=None):
    # out_name を指定すると、出力ファイル名を config の slug と切り離して
    # 自由に決められる(configのslugはあくまで configs/<slug>.json という
    # 保存先ファイル名としてのみ使われる)。
    slug = out_name or config.get("slug", "race")
    route_list = list(paths.values())
    motions = {r["key"]: RouteMotion(r) for r in route_list}

    intro_sec = config.get("intro_sec", 2.0)
    ratio = config.get("compress_sec_per_min", 0.5)
    outro_hold = config.get("outro_hold_sec", OUTRO_HOLD_SEC)

    timeline = build_events(config, paths, intro_sec, ratio)
    write_script_files(timeline, config, paths, out_dir=out_dir)
    # 実況テロップを動画内に焼き込むかどうか。台本(.txt)・字幕(.srt)ファイルは
    # show_captions の設定に関わらず常に output/ に書き出される
    # (ナレーション収録や動画編集ソフトでの字幕付けに使える)。
    show_captions = bool(config.get("show_captions", False))

    icons = {r["key"]: load_icon(r_cfg) for r, r_cfg in zip(route_list, config["routes"])}

    total_video_sec = timeline[-1]["t_end"] + outro_hold
    fps = 10 if fast_preview else fps

    if os.path.exists(frames_dir):
        shutil.rmtree(frames_dir)
    os.makedirs(frames_dir)

    n_frames = int(total_video_sec * fps)
    print(f"rendering {n_frames} frames ({total_video_sec:.1f}s @ {fps}fps) ...")

    result_start = max(m.total_min for m in motions.values()) * ratio + intro_sec + 1.0
    color_by_key = {r["key"]: tuple(r["color"]) for r in route_list}

    for fi in range(n_frames):
        t = fi / fps
        canvas = base_map_rgba.copy()

        real_mins = {}
        for r in route_list:
            m = motions[r["key"]]
            race_t = t - intro_sec
            real_min = 0.0 if race_t < 0 else race_t / ratio
            real_mins[r["key"]] = min(real_min, m.total_min)

        for r in reversed(route_list):
            draw_progress_route(canvas, proj, r, motions[r["key"]], real_mins[r["key"]], color_by_key[r["key"]])

        for r in reversed(route_list):
            lon, lat = motions[r["key"]].lonlat_at(real_mins[r["key"]])
            x, y = project(proj, lon, lat)
            draw_train_icon(canvas, x, y, color_by_key[r["key"]], icon_img=icons[r["key"]])

        for r in route_list:
            for st in r["stations"]:
                if not st["popup"]:
                    continue
                t_reach = intro_sec + st["t_min"] * ratio
                elapsed = t - t_reach
                if 0 <= elapsed <= 1.6:
                    draw_popup(canvas, proj, st, elapsed, color_by_key[r["key"]])

        draw_scoreboard(canvas, route_list, motions, real_mins)

        if show_captions:
            for ev in timeline:
                if ev["t_start"] <= t <= ev["t_end"]:
                    fade = 1.0
                    if t - ev["t_start"] < 0.15:
                        fade = (t - ev["t_start"]) / 0.15
                    elif ev["t_end"] - t < 0.15:
                        fade = (ev["t_end"] - t) / 0.15
                    color = color_by_key.get(ev["speaker_key"], (255, 215, 0))
                    draw_caption(canvas, ev["text"], ev["speaker"], color, alpha=int(255 * max(0, fade)))
                    break

        if t >= result_start:
            a = min(1.0, (t - result_start) / 0.8)
            draw_result_panel(canvas, config, route_list, alpha=int(255 * a))

        # PNG保存はデフォルト圧縮だとCPUコストが大きく、フレーム書き出し全体の
        # 最大のボトルネックになっていた(プロファイルで確認済み)。この連番PNG
        # はffmpegに渡した後すぐ捨てる中間ファイルなので、圧縮率より速度を優先
        # する(compress_level=1)。CPUが弱い環境(Renderの無料プランなど)での
        # 生成時間を大きく縮められる。
        canvas.convert("RGB").save(f"{frames_dir}/frame_{fi:05d}.png", compress_level=1)
        if fi % 60 == 0:
            print(f"  frame {fi}/{n_frames}")

    print("encoding mp4 ...")
    out_path = f"{out_dir}/{slug}.mp4"
    cmd = [
        "ffmpeg", "-y", "-framerate", str(fps),
        "-i", f"{frames_dir}/frame_%05d.png",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-vf", "format=yuv420p",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    print("done:", out_path)
    return out_path
