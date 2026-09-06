# 電車最速バトル動画ジェネレーター (train-route-race-video)

首都圏の路線図データ(GeoJSON/駅データSHP)を使って、「A駅からB駅まで、
どの路線が一番速いか」を比較する縦動画(1080x1920, ショート動画向け)を
自動生成するツールです。地図上で2つの路線が実際のルート形状に沿って
レースし、通過駅ポップアップ・スコアボード・競馬実況風のテロップ、
実況台本(.txt / .srt)まで一括で作ります。

新宿→藤沢(小田急線 vs JR湘南新宿ライン)の比較動画を題材に作りましたが、
`configs/*.json` を書けば**どの2駅・どの路線の組み合わせでも**汎用的に
使えるように設計してあります。

## できること

- 実在の路線形状(GeoJSON)に沿って2つの経路をレースさせるアニメーション
- 車両アイコンは回転しない(独自イラストへの差し替え可能)
- 主要駅を通過すると「大きく→通常サイズ」でポップアップ表示
- 下部にスコアボード(進捗バー・経過分数)
- 競馬実況風のテロップをテンプレートから自動生成し、動画に焼き込み
- 実況の台本(.txt)と字幕(.srt)を別ファイルでも出力
- 駅データ(SHP)の品質チェックツール付き(後述の「既知の注意点」を参照)

## セットアップ

```bash
git clone <このリポジトリのURL>
cd train-route-race-video

# Python依存パッケージ
pip install -r requirements.txt

# ffmpeg(動画エンコードに必須。pipには無いのでOS側で入れる)
sudo apt-get update && sudo apt-get install -y ffmpeg

# 日本語フォント(Noto Sans CJK)
sudo apt-get install -y fonts-noto-cjk
```

macOSの場合は `brew install ffmpeg` と、日本語フォントは標準搭載のものが
自動的に見つからない場合、`race_video/fonts.py` の `_CANDIDATE_DIRS` に
パスを足すか、環境変数 `RACE_VIDEO_FONT_DIR` でフォントの入っている
ディレクトリを指定してください。

## 使い方

```bash
# 動画生成(30fps, フル画質)
python3 -m race_video.cli configs/shinjuku_fujisawa.json

# 低fpsの高速プレビュー(内容確認用、10fps)
python3 -m race_video.cli configs/shinjuku_fujisawa.json --fast

# 駅データSHPの品質だけチェックしたいとき
python3 -m race_video.cli configs/shinjuku_fujisawa.json --check-stations
```

生成物は `output/` に出力されます:

- `output/<slug>.mp4` … 完成動画
- `output/<slug>_base_map.png` … 背景マップ単体(確認用)
- `output/<slug>_commentary.txt` … 実況台本(タイムコード付き)
- `output/<slug>_commentary.srt` … 字幕ファイル

## 他のルートを追加する方法(汎用化のポイント)

新しい比較動画を作るには `configs/` に新しいJSONを1つ足すだけです。
`configs/shinjuku_fujisawa.json` をコピーして書き換えてください。

```jsonc
{
  "slug": "shibuya_yokohama",              // 出力ファイル名に使われる
  "title_line1": "渋谷 → 横浜",
  "title_line2": "どっちが早い!? 電車最速バトル",
  "start_name": "渋谷",
  "end_label": "横浜",
  "compress_sec_per_min": 0.5,             // 実1分を動画何秒に圧縮するか
  "intro_sec": 2.0,
  "outro_hold_sec": 4.0,
  "routes": [
    {
      "key": "route_a",                    // ルートを識別するキー(a/bなど)
      "name": "東急東横線",
      "short_name": "東急東横線",           // スコアボード等で使う短縮名
      "color": [230, 20, 20],
      "icon_path": "assets/toyoko_icon.png", // 省略可(無ければ簡易アイコン)
      "stations": [
        {"line": "東急東横線", "name": "渋谷", "t_min": 0,  "popup": true},
        {"line": "東急東横線", "name": "武蔵小杉", "t_min": 12, "popup": true},
        {"line": "東急東横線", "name": "横浜", "t_min": 26, "popup": true}
      ]
    },
    { "key": "route_b", "name": "...", "stations": [ ... ] }
  ]
}
```

駅の「並び順」は `stations` 配列の順番がそのまま使われます。`line` には
駅データSHPの `line_name` と完全一致する路線名を指定してください
(一致しないとエラーで教えてくれます)。`t_min` は始発駅からの累積所要時間
(分)で、乗換案内サイト等の実データを参考に入れてください。

途中で別の路線に乗り換える(直通運転含む)場合は、単に `stations` の
`line` を次の駅から新しい路線名に変えるだけです。乗換駅には
`"transfer": true, "transfer_note": "..."` を、乗換なしの直通運転駅には
`"direct_through": true, "transfer_note": "..."` を付けると、実況テロップの
文面が変わります。

`popup: true` を付けた駅だけが動画中にポップアップ表示され、実況の
対象にもなります(あまり多いと画面が窮屈になるので、主要駅だけ絞るのが
おすすめです)。

## データについて

- `data/routes.geojson` … 首都圏52路線の線形状(LineString/MultiLineString)
- `data/stations_shp/tokyo_stations.*` … 61路線分の駅データ(Shapefile。
  フィールドは `line_name, stn_name, seq, cum_km, color`)

### 既知の注意点(駅データSHPの品質)

`python3 -m race_video.cli <config> --check-stations` で全路線の品質を
チェックできます。61路線中59路線は駅の並び順(seq)・距離(cum_km)・座標が
正確でしたが、以下の2路線だけ複数の物理系統(例: 小田原方面と宇都宮方面)
の駅が1本のseq/cum_kmに混線しており、**並び順をそのまま信用できません**
(駅名→座標の対応自体は正しいので、座標の引き当てには使えます)。

- 湘南新宿ライン
- 上野東京ライン

原因は、これらの路線が `routes.geojson` 上でMultiLineString(=複数の
物理系統に分かれた線形状)になっており、系統をまたいで駅が最近傍の
頂点にマッチングされてしまったためと推測されます。この2路線を経路に
含める場合は、本ツールの設計どおり `stations` 配列で実際の停車順を
明示的に指定してください(`configs/shinjuku_fujisawa.json` のJR側が実例です)。

将来的にSHP生成スクリプト側を直すなら、「系統(LineStringのセグメント)
ごとに」駅を割り当て直す形にすると、この2路線も自動生成に使えるように
なるはずです。

## 車両アイコンについて

`icon_path` に透過PNG(進行方向=右向き推奨)を指定すると、その画像が
車両アイコンとして使われます。**回転はさせません**(要件どおり、常に
同じ向きで表示されます)。指定が無い場合は `race_video/animate.py` が
簡易なプレースホルダーアイコンを描画します。`assets/` に画像を置いて
config から参照してください。

## 今後の拡張(バックエンド化)

- `race_video/route_builder.py` の `build_all_routes()` が「発駅・着駅
  ->経路」の中核ロジックです。Webアプリのバックエンドに組み込む場合は、
  ユーザー入力(発駅・着駅)から `stations` 配列を自動組み立てする層
  (経路探索)を追加し、`build_all_routes()` 以降はそのまま再利用できます。
- フロントエンドから配信する場合は、`animate.py` の `render()` を
  ジョブキュー(Celery/RQ等)やサーバーレス関数から呼び出す形にすると
  Renderなどにそのままデプロイできます。

## Renderへのデプロイに関する補足

Renderの `Background Worker` または `Web Service` で、Dockerイメージに
`ffmpeg` と `fonts-noto-cjk` を含めてビルドしてください(Renderの
Native Environmentにはこれらが入っていないため、Dockerfileでの導入を
推奨します)。動画1本の生成に数分かかるため、同期HTTPリクエストの
レスポンスとして返すのではなく、ジョブを非同期実行してURLを返す設計に
するのが安全です。
