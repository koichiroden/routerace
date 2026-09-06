# -*- coding: utf-8 -*-
"""
Renderなどのホスティング上でブラウザから動画生成を実行するための、
ごく簡単なWebラッパー。

- configs/*.json を一覧表示し、ボタンを押すとバックグラウンドで
  `python3 -m race_video.cli configs/<slug>.json` を実行する。
- 生成状況はポーリングで確認し、完了したらそのままブラウザから
  mp4をダウンロードできる。

ローカルで試す場合:
    pip install -r requirements.txt
    python3 web/app.py
    -> http://127.0.0.1:8080 を開く
"""
import json
import os
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIGS_DIR = BASE_DIR / "configs"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)

jobs = {}
jobs_lock = threading.Lock()


def list_configs():
    items = []
    for p in sorted(CONFIGS_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            title = f"{cfg.get('title_line1', p.stem)} {cfg.get('title_line2', '')}".strip()
        except Exception:
            title = p.stem
        items.append({"slug": p.stem, "title": title})
    return items


def existing_videos():
    return {p.stem for p in OUTPUT_DIR.glob("*.mp4")}


def run_job(job_id, slug, fast):
    cfg_path = CONFIGS_DIR / f"{slug}.json"
    cmd = ["python3", "-m", "race_video.cli", str(cfg_path)]
    if fast:
        cmd.append("--fast")
    try:
        proc = subprocess.run(
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=1800,
        )
        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        with jobs_lock:
            jobs[job_id]["log"] = log[-4000:]
            if proc.returncode == 0 and (OUTPUT_DIR / f"{slug}.mp4").exists():
                jobs[job_id]["status"] = "done"
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"終了コード {proc.returncode}(ログを参照)"
    except Exception as e:  # noqa: BLE001
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)


PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>電車最速バトル 動画生成</title>
<style>
  body { font-family: -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif;
         max-width: 760px; margin: 40px auto; padding: 0 20px; color: #201c16; background: #f6f2ea; }
  h1 { font-size: 22px; }
  .card { background: #fff; border: 1px solid #ded2ba; border-radius: 10px;
          padding: 16px 18px; margin-bottom: 16px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .title { font-weight: 600; }
  .slug { color: #766c5c; font-size: 12.5px; font-family: monospace; }
  button { font: inherit; padding: 8px 14px; border-radius: 7px; border: 1px solid #201c16;
           background: #201c16; color: #f6f2ea; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  label.fast { font-size: 12.5px; color: #766c5c; display: flex; align-items: center; gap: 4px; }
  .status { font-size: 13px; margin-top: 8px; color: #766c5c; }
  .status.ok { color: #21785a; }
  .status.err { color: #b23b3b; white-space: pre-wrap; }
  a.dl { color: #1a56c9; font-weight: 600; text-decoration: none; }
  .empty { color: #9c907c; }
</style>
</head>
<body>
  <h1>電車最速バトル — 動画生成</h1>
  <p>configs/ にあるレース設定を選んで生成できます。生成には数分かかります。</p>
  {% if not configs %}
    <p class="empty">configs/ にJSONファイルが見つかりません。</p>
  {% endif %}
  {% for c in configs %}
  <div class="card" data-slug="{{ c.slug }}">
    <div class="row">
      <div>
        <div class="title">{{ c.title }}</div>
        <div class="slug">{{ c.slug }}.json</div>
      </div>
      <div style="display:flex; align-items:center; gap:10px;">
        <label class="fast"><input type="checkbox" class="fast-cb"> 低画質プレビュー(高速)</label>
        <button class="gen-btn">生成する</button>
      </div>
    </div>
    <div class="status"></div>
  </div>
  {% endfor %}

<script>
document.querySelectorAll(".card").forEach(card => {
  const slug = card.dataset.slug;
  const btn = card.querySelector(".gen-btn");
  const statusEl = card.querySelector(".status");
  const fastCb = card.querySelector(".fast-cb");

  function poll(jobId) {
    fetch("/status/" + jobId).then(r => r.json()).then(job => {
      if (job.status === "running") {
        statusEl.className = "status";
        statusEl.textContent = "生成中…(数分かかります)";
        setTimeout(() => poll(jobId), 3000);
      } else if (job.status === "done") {
        statusEl.className = "status ok";
        statusEl.innerHTML = "完成しました → <a class='dl' href='/download/" + slug + "'>" + slug + ".mp4 をダウンロード</a>";
        btn.disabled = false;
        btn.textContent = "もう一度生成する";
      } else {
        statusEl.className = "status err";
        statusEl.textContent = "エラー: " + (job.error || "不明なエラー") + "\\n\\n" + (job.log || "");
        btn.disabled = false;
        btn.textContent = "再試行";
      }
    }).catch(() => setTimeout(() => poll(jobId), 4000));
  }

  btn.addEventListener("click", () => {
    btn.disabled = true;
    btn.textContent = "生成中…";
    statusEl.className = "status";
    statusEl.textContent = "開始しています…";
    const body = new URLSearchParams();
    if (fastCb.checked) body.set("fast", "1");
    fetch("/generate/" + slug, { method: "POST", body })
      .then(r => r.json())
      .then(data => {
        if (data.job_id) poll(data.job_id);
        else { statusEl.className = "status err"; statusEl.textContent = data.error || "開始できませんでした"; btn.disabled = false; }
      });
  });
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, configs=list_configs())


@app.route("/generate/<slug>", methods=["POST"])
def generate(slug):
    valid = {c["slug"] for c in list_configs()}
    if slug not in valid:
        return jsonify({"error": "そのconfigは見つかりません"}), 404
    fast = request.form.get("fast") == "1"
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running", "slug": slug, "log": "", "error": ""}
    threading.Thread(target=run_job, args=(job_id, slug, fast), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.route("/download/<slug>")
def download(slug):
    path = OUTPUT_DIR / f"{slug}.mp4"
    if not path.exists():
        return "まだ生成されていません", 404
    return send_file(path, as_attachment=True, download_name=f"{slug}.mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
