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
import re
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIGS_DIR = BASE_DIR / "configs"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Render無料プラン(0.1 CPU)は非常に遅いため、フルクオリティ(30fps)の生成は
# 数十分かかることがある。あまりに小さいと正常なフルクオリティ生成まで
# 中断してしまうので、安全マージンを広めに取っておく。
JOB_TIMEOUT_SEC = 2700

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=JOB_TIMEOUT_SEC,
        )
        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        with jobs_lock:
            jobs[job_id]["log"] = log[-4000:]
            if proc.returncode == 0 and (OUTPUT_DIR / f"{slug}.mp4").exists():
                jobs[job_id]["status"] = "done"
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"終了コード {proc.returncode}(ログを参照)"
    except subprocess.TimeoutExpired as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["log"] = ((e.stdout or "") + "\n" + (e.stderr or ""))[-4000:]
            jobs[job_id]["error"] = (
                f"{JOB_TIMEOUT_SEC}秒以内に終わりませんでした。"
                f"無料プランのCPUは非常に弱いため、フルクオリティの生成は間に合わないことがあります。"
                f"「低画質プレビュー(高速)」を試すか、有料プランへの変更を検討してください。"
            )
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
  h2.section { font-size: 15px; margin: 28px 0 10px; }
  .note { font-size: 12.5px; color: #a3690a; background: #f8ecd6; border-radius: 7px;
          padding: 8px 10px; margin: 0 0 16px; }
  .card { background: #fff; border: 1px solid #ded2ba; border-radius: 10px;
          padding: 16px 18px; margin-bottom: 16px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .title { font-weight: 600; }
  .slug { color: #766c5c; font-size: 12.5px; font-family: monospace; }
  button { font: inherit; padding: 8px 14px; border-radius: 7px; border: 1px solid #201c16;
           background: #201c16; color: #f6f2ea; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  label.fast { font-size: 12.5px; color: #766c5c; display: flex; align-items: center; gap: 4px; }
  .status { font-size: 13px; margin-top: 8px; color: #766c5c; white-space: pre-wrap; }
  .status.ok { color: #21785a; }
  .status.err { color: #b23b3b; }
  a.dl { color: #1a56c9; font-weight: 600; text-decoration: none; }
  .empty { color: #9c907c; }
  textarea.jsonbox { width: 100%; min-height: 160px; font-family: monospace; font-size: 12.5px;
                      padding: 10px; border-radius: 7px; border: 1px solid #ded2ba; box-sizing: border-box;
                      background: #fbf8f2; color: #201c16; }
  .paste-actions { display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
</style>
</head>
<body>
  <h1>電車最速バトル — 動画生成</h1>
  <p class="note">無料プランはCPUが非常に弱いため、まずは「低画質プレビュー(高速)」での生成をおすすめします。</p>

  <h2 class="section">① JSONを貼り付けて生成</h2>
  <div class="card">
    <p style="margin-top:0; font-size:13px; color:#766c5c;">
      Claudeとのチャットなどで作ったconfig JSONをそのまま貼り付けて生成できます
      (<span class="slug">configs/&lt;slug&gt;.json</span> として保存されます)。
    </p>
    <textarea class="jsonbox" id="paste-json" placeholder='{"slug": "...", "routes": [...] }'></textarea>
    <div class="paste-actions">
      <label class="fast"><input type="checkbox" id="paste-fast" checked> 低画質プレビュー(高速)</label>
      <button id="paste-btn">保存して生成する</button>
    </div>
    <div class="status" id="paste-status"></div>
  </div>

  <h2 class="section">② 保存済みの設定から生成</h2>
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
        <label class="fast"><input type="checkbox" class="fast-cb" checked> 低画質プレビュー(高速)</label>
        <button class="gen-btn">生成する</button>
      </div>
    </div>
    <div class="status"></div>
  </div>
  {% endfor %}

<script>
function pollJob(jobId, slug, statusEl, btn, doneLabel) {
  fetch("/status/" + jobId).then(r => r.json()).then(job => {
    if (job.status === "running") {
      statusEl.className = "status";
      statusEl.textContent = "生成中…(無料プランだと低画質プレビューでも数分~十数分かかることがあります)";
      setTimeout(() => pollJob(jobId, slug, statusEl, btn, doneLabel), 3000);
    } else if (job.status === "done") {
      statusEl.className = "status ok";
      statusEl.innerHTML = "完成しました → <a class='dl' href='/download/" + slug + "'>" + slug + ".mp4 をダウンロード</a>";
      btn.disabled = false;
      btn.textContent = doneLabel;
    } else {
      statusEl.className = "status err";
      statusEl.textContent = "エラー: " + (job.error || "不明なエラー") + "\\n\\n" + (job.log || "");
      btn.disabled = false;
      btn.textContent = "再試行";
    }
  }).catch(() => setTimeout(() => pollJob(jobId, slug, statusEl, btn, doneLabel), 4000));
}

document.querySelectorAll(".card[data-slug]").forEach(card => {
  const slug = card.dataset.slug;
  const btn = card.querySelector(".gen-btn");
  const statusEl = card.querySelector(".status");
  const fastCb = card.querySelector(".fast-cb");

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
        if (data.job_id) pollJob(data.job_id, slug, statusEl, btn, "もう一度生成する");
        else { statusEl.className = "status err"; statusEl.textContent = data.error || "開始できませんでした"; btn.disabled = false; btn.textContent = "生成する"; }
      });
  });
});

document.getElementById("paste-btn").addEventListener("click", () => {
  const btn = document.getElementById("paste-btn");
  const statusEl = document.getElementById("paste-status");
  const text = document.getElementById("paste-json").value;
  const fast = document.getElementById("paste-fast").checked;
  btn.disabled = true;
  btn.textContent = "保存しています…";
  statusEl.className = "status";
  statusEl.textContent = "";
  const body = new URLSearchParams();
  body.set("json_text", text);
  if (fast) body.set("fast", "1");
  fetch("/generate_from_json", { method: "POST", body })
    .then(r => r.json())
    .then(data => {
      if (data.job_id) {
        btn.textContent = "生成中…";
        pollJob(data.job_id, data.slug, statusEl, btn, "もう一度保存して生成する");
      } else {
        statusEl.className = "status err";
        statusEl.textContent = data.error || "開始できませんでした";
        btn.disabled = false;
        btn.textContent = "保存して生成する";
      }
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


@app.route("/generate_from_json", methods=["POST"])
def generate_from_json():
    raw = request.form.get("json_text", "")
    if not raw.strip():
        return jsonify({"error": "JSONを貼り付けてください"}), 400
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSONの構文エラー: {e}"}), 400

    if not isinstance(config, dict) or not config.get("routes"):
        return jsonify({"error": "'routes' を含むconfigオブジェクトではないようです"}), 400

    slug = str(config.get("slug", "")).strip()
    if not slug:
        return jsonify({"error": "configに 'slug' が必要です"}), 400
    if not _SLUG_RE.match(slug):
        return jsonify({"error": "slugは英数字とハイフン・アンダースコアのみ使えます(64文字以内)"}), 400

    cfg_path = CONFIGS_DIR / f"{slug}.json"
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return jsonify({"error": f"保存に失敗しました: {e}"}), 500

    fast = request.form.get("fast") == "1"
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running", "slug": slug, "log": "", "error": ""}
    threading.Thread(target=run_job, args=(job_id, slug, fast), daemon=True).start()
    return jsonify({"job_id": job_id, "slug": slug})


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
