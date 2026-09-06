# -*- coding: utf-8 -*-
"""
レース設定(config)から、実際の路線形状に沿ったポリライン + 駅位置 + 累積距離
を組み立てる。どの2駅間・どの路線の組み合わせでも使えるように汎用化してある。

config の1ルートは以下の形:
{
  "key": "route_a",
  "name": "小田急線",
  "color": [0,110,235],           # 省略時は station_db の色を使う
  "stations": [
     {"line": "小田急小田原線", "name": "新宿", "t_min": 0, "popup": true},
     {"line": "小田急小田原線", "name": "登戸", "t_min": 18, "popup": true},
     ...
     {"line": "小田急江ノ島線", "name": "藤沢", "t_min": 58, "popup": true,
      "transfer": true, "transfer_note": "直通(乗換なし)"},
  ]
}
駅の並び順(= どの駅をどの順で通るか)は、この stations 配列の順序で明示的に
指定する。station_db 側のseq/cum_kmが壊れている路線(湘南新宿ラインなど)
でも、駅名->座標の引き当てさえ合っていれば問題なく使える。

各駅の "line" が routes.geojson に無い路線名の場合(駅データSHPには
あるが線形状データが無い路線)は、前後の駅を直線で結ぶフォールバックになる。
"""
import json

from .station_db import haversine_km, get_station_lonlat


def load_line_geoms(geojson_path):
    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)
    lines = {}
    for feat in data["features"]:
        name = feat["properties"]["line_name"]
        geom = feat["geometry"]
        if geom["type"] == "LineString":
            lines[name] = [geom["coordinates"]]
        else:  # MultiLineString
            lines[name] = geom["coordinates"]
    return lines


def nearest_index(coords, pt):
    best_i, best_d = 0, float("inf")
    for i, c in enumerate(coords):
        d = haversine_km(c, pt)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i, best_d


def pick_best_segment(segments, pt):
    best = None
    for seg in segments:
        i, d = nearest_index(seg, pt)
        if best is None or d < best[2]:
            best = (seg, i, d)
    return best


def cumulative_dist(coords):
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_km(coords[i - 1], coords[i]))
    return cum


def build_route(route_cfg, line_geoms, station_db):
    """1ルート分のポリライン・駅情報を組み立てる。戻り値と warnings のタプル。"""
    stations_cfg = route_cfg["stations"]
    warnings = []

    resolved = []
    for s in stations_cfg:
        try:
            lon, lat = get_station_lonlat(station_db, s["line"], s["name"])
        except KeyError as e:
            raise KeyError(
                f"[{route_cfg.get('name', route_cfg.get('key'))}] {e}. "
                f"station_db.load_station_db() のline_name表記(路線名)と "
                f"config内の'line'が一致しているか確認してください。"
            )
        resolved.append({**s, "lon": lon, "lat": lat})

    # 連続して同じ路線の駅が続く区間ごとにグループ化する
    seg_ranges = []
    cur_line = resolved[0]["line"]
    start_i = 0
    for i in range(1, len(resolved)):
        if resolved[i]["line"] != cur_line:
            seg_ranges.append((cur_line, start_i, i - 1))
            cur_line = resolved[i]["line"]
            start_i = i - 1  # 前の駅を継ぎ目として重複させる
    seg_ranges.append((cur_line, start_i, len(resolved) - 1))

    polyline = []
    for line_name, si, ei in seg_ranges:
        pt_start = (resolved[si]["lon"], resolved[si]["lat"])
        pt_end = (resolved[ei]["lon"], resolved[ei]["lat"])

        segments = line_geoms.get(line_name)
        if not segments:
            warnings.append(
                f"'{line_name}' の線形状データが routes.geojson に無いため、"
                f"{resolved[si]['name']}-{resolved[ei]['name']} 間は直線で結びます。"
            )
            piece = [pt_start, pt_end]
        else:
            seg_coords, idx_start, d0 = pick_best_segment(segments, pt_start)
            idx_end, d1 = nearest_index(seg_coords, pt_end)
            if d0 > 1.0:
                warnings.append(f"{resolved[si]['name']} のスナップ誤差 {d0:.2f}km")
            if d1 > 1.0:
                warnings.append(f"{resolved[ei]['name']} のスナップ誤差 {d1:.2f}km")
            if idx_start <= idx_end:
                piece = seg_coords[idx_start:idx_end + 1]
            else:
                piece = list(reversed(seg_coords[idx_end:idx_start + 1]))

        if polyline and piece and polyline[-1] == piece[0]:
            piece = piece[1:]
        polyline.extend(piece)

    cum = cumulative_dist(polyline)

    station_records = []
    for s in resolved:
        idx, d = nearest_index(polyline, (s["lon"], s["lat"]))
        station_records.append({
            "name": s["name"],
            "line": s["line"],
            "lon": polyline[idx][0],
            "lat": polyline[idx][1],
            "t_min": s["t_min"],
            "path_index": idx,
            "cum_km": cum[idx],
            "popup": bool(s.get("popup", False)),
            "transfer": bool(s.get("transfer", False)),
            "direct_through": bool(s.get("direct_through", False)),
            "transfer_note": s.get("transfer_note", ""),
        })

    idxs = [s["path_index"] for s in station_records]
    if idxs != sorted(idxs):
        warnings.append(
            f"{route_cfg.get('name')}: 駅の path_index が単調増加していません: {idxs} "
            f"(config内の駅の並び順が実際の進行方向と逆になっている可能性があります)"
        )

    total_min = stations_cfg[-1]["t_min"]
    color = route_cfg.get("color") or [230, 230, 230]

    result = {
        "key": route_cfg["key"],
        "name": route_cfg["name"],
        "short_name": route_cfg.get("short_name", route_cfg["name"]),
        "color": color,
        "total_min": total_min,
        "polyline": polyline,
        "cum_dist": cum,
        "stations": station_records,
    }
    return result, warnings


def build_all_routes(config, geojson_path="data/routes.geojson",
                      station_db=None):
    from .station_db import load_station_db
    if station_db is None:
        station_db = load_station_db()
    line_geoms = load_line_geoms(geojson_path)

    paths = {}
    all_warnings = []
    for route_cfg in config["routes"]:
        route, warnings = build_route(route_cfg, line_geoms, station_db)
        paths[route_cfg["key"]] = route
        all_warnings.extend(warnings)
    return paths, all_warnings


if __name__ == "__main__":
    import sys
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/shinjuku_fujisawa.json"
    with open(cfg_path, encoding="utf-8") as f:
        config = json.load(f)
    paths, warnings = build_all_routes(config)
    for w in warnings:
        print("[warn]", w)
    for key, route in paths.items():
        print(key, route["name"], "points:", len(route["polyline"]),
              "total_km:", round(route["cum_dist"][-1], 2), "total_min:", route["total_min"])
        for s in route["stations"]:
            print("   ", s["name"], "idx", s["path_index"], "km", round(s["cum_km"], 2), "t", s["t_min"])
