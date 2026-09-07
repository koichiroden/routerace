# -*- coding: utf-8 -*-
"""
ブラウザから動画生成を実行するための、ごく簡単なWebラッパー。
自分のPC上でも(`python3 web/app.py`)、Renderなどのホスティング上でも
同じように動く。

- 駅レースビルダー(Claude Artifact)で作ったJSONを貼り付け/ファイル選択で
  読み込み、バックグラウンドで `python3 -m race_video.cli configs/<slug>.json`
  を実行する。
- configs/*.json に保存済みの設定からも生成できる。
- 生成状況はポーリングで確認し、完了したらそのままブラウザから
  mp4をダウンロードできる。
- 出力ファイル名は、config の 'slug'(=保存先ファイル名)とは別に、
  生成のたびに自由に指定できる(空欄ならslugがそのまま使われる)。

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

# 自分のPCならもっと速く終わるはずだが、Renderの無料プラン(0.1 CPU)などの
# 非力な環境だとフルクオリティ(30fps)の生成に数十分かかることがあるため、
# 安全マージンを広めに取っておく。
JOB_TIMEOUT_SEC = 2700

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OUT_NAME_BAD_RE = re.compile(r"[\\/\x00-\x1f]")

app = Flask(__name__)

jobs = {}
jobs_lock = threading.Lock()

# サーバーが生成の途中で再起動されてしまった場合(無料プランのメモリ/CPU制限
# など)、ジョブ情報はメモリ上の jobs dict ごと消えてしまい、ポーリング中の
# ブラウザには "not found" とだけ返ってしまって原因が分かりにくい。
# job_id -> {slug, file_name} の対応表だけは軽量なファイルに書き出しておき、
# jobsから消えていた場合でも「サーバーが再起動した可能性がある」という
# 分かりやすいメッセージと、出力ファイルが実際にできていれば復旧できるように
# しておく。
JOBS_STATE_PATH = OUTPUT_DIR / "_jobs_state.json"
_jobs_state_lock = threading.Lock()


def _load_jobs_state():
    try:
        with open(JOBS_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _remember_job(job_id, slug, file_name):
    with _jobs_state_lock:
        state = _load_jobs_state()
        state[job_id] = {"slug": slug, "file_name": file_name}
        if len(state) > 50:  # 肥大化防止に直近50件だけ残す
            for k in list(state.keys())[:-50]:
                state.pop(k, None)
        try:
            with open(JOBS_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except OSError:
            pass


def _sanitize_out_name(raw):
    """出力ファイル名(拡張子なし)として安全かどうかを確認する。
    戻り値は (name, error) のタプル。
    空欄は「指定なし(configのslugを使う)」という意味で ("", None) を返す。
    使えない文字が含まれる場合は (None, エラーメッセージ) を返す。"""
    name = (raw or "").strip()
    if not name:
        return "", None
    if len(name) > 100 or _OUT_NAME_BAD_RE.search(name) or ".." in name or name.strip(".") == "":
        return None, "出力ファイル名に使えない文字が含まれています(/ や \\、'..' は使えません)"
    return name, None


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


def run_job(job_id, slug, fast, out_name=None):
    cfg_path = CONFIGS_DIR / f"{slug}.json"
    file_stem = out_name or slug
    cmd = ["python3", "-m", "race_video.cli", str(cfg_path)]
    if fast:
        cmd.append("--fast")
    if out_name:
        cmd += ["--out-name", out_name]
    try:
        proc = subprocess.run(
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=JOB_TIMEOUT_SEC,
        )
        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        with jobs_lock:
            jobs[job_id]["log"] = log[-4000:]
            if proc.returncode == 0 and (OUTPUT_DIR / f"{file_stem}.mp4").exists():
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
                f"CPUが非力な環境だと、フルクオリティの生成は間に合わないことがあります。"
                f"「低画質プレビュー(高速)」を試すか、より速いPC/有料プランを検討してください。"
            )
    except Exception as e:  # noqa: BLE001
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)


def _start_job(slug, fast, out_name_raw):
    """out_nameのバリデーション込みでジョブを開始する共通処理。
    戻り値は (response_dict, http_status)。"""
    out_name, err = _sanitize_out_name(out_name_raw)
    if err:
        return {"error": err}, 400
    file_name = f"{out_name or slug}.mp4"
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running", "slug": slug, "file_name": file_name, "log": "", "error": ""}
    _remember_job(job_id, slug, file_name)
    threading.Thread(target=run_job, args=(job_id, slug, fast, out_name or None), daemon=True).start()
    return {"job_id": job_id, "slug": slug, "file_name": file_name}, 200


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
  input.outname { font: inherit; font-size: 12.5px; padding: 6px 8px; border-radius: 6px;
                   border: 1px solid #ded2ba; background: #fbf8f2; color: #201c16; width: 200px; }
  label.outname-label { font-size: 12.5px; color: #766c5c; display: flex; align-items: center; gap: 6px; }
  input[type="file"] { font-size: 12.5px; }
</style>
</head>
<body>
  <h1>電車最速バトル — 動画生成</h1>
  <p class="note">自分のPCで動かす場合はフルクオリティでも数分で終わります。CPUが非力な環境(無料ホスティングなど)では、まず「低画質プレビュー(高速)」をおすすめします。</p>

  <h2 class="section">① JSONを用意して生成</h2>
  <div class="card">
    <p style="margin-top:0; font-size:13px; color:#766c5c;">
      駅レースビルダーで作ったconfig JSONを、ファイルから読み込むか、貼り付けて生成できます
      (<span class="slug">configs/&lt;slug&gt;.json</span> として保存されます)。
    </p>
    <div class="paste-actions" style="margin-top:0; margin-bottom:8px;">
      <input type="file" id="file-input" accept=".json,application/json">
      <button id="clip-btn" type="button">📋 クリップボードから貼り付け</button>
      <span class="status" id="clip-status" style="margin-top:0;"></span>
    </div>
    <textarea class="jsonbox" id="paste-json" placeholder='{"slug": "...", "routes": [...] }'></textarea>
    <div class="paste-actions">
      <label class="fast"><input type="checkbox" id="paste-fast" checked> 低画質プレビュー(高速)</label>
      <label class="outname-label">出力ファイル名(任意)
        <input type="text" class="outname" id="paste-outname" placeholder="省略時はslugを使用">
      </label>
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
    </div>
    <div class="paste-actions">
      <label class="fast"><input type="checkbox" class="fast-cb" checked> 低画質プレビュー(高速)</label>
      <label class="outname-label">出力ファイル名(任意)
        <input type="text" class="outname" placeholder="省略時は {{ c.slug }} を使用">
      </label>
      <button class="gen-btn">生成する</button>
    </div>
    <div class="status"></div>
  </div>
  {% endfor %}

<script>
function pollJob(jobId, statusEl, btn, doneLabel) {
  fetch("/status/" + jobId).then(r => r.json()).then(job => {
    if (job.status === "running") {
      statusEl.className = "status";
      statusEl.textContent = "生成中…(CPUが非力な環境だと数分~十数分かかることがあります)";
      setTimeout(() => pollJob(jobId, statusEl, btn, doneLabel), 3000);
    } else if (job.status === "done") {
      const fname = job.file_name || "output.mp4";
      statusEl.className = "status ok";
      statusEl.innerHTML = "完成しました → <a class='dl' href='/download/" +
        encodeURIComponent(fname.replace(/\\.mp4$/, "")) + "'>" + fname + " をダウンロード</a>";
      btn.disabled = false;
      btn.textContent = doneLabel;
    } else {
      statusEl.className = "status err";
      statusEl.textContent = "エラー: " + (job.error || "不明なエラー") + "\\n\\n" + (job.log || "");
      btn.disabled = false;
      btn.textContent = "再試行";
    }
  }).catch(() => setTimeout(() => pollJob(jobId, statusEl, btn, doneLabel), 4000));
}

document.querySelectorAll(".card[data-slug]").forEach(card => {
  const slug = card.dataset.slug;
  const btn = card.querySelector(".gen-btn");
  const statusEl = card.querySelector(".status");
  const fastCb = card.querySelector(".fast-cb");
  const outnameInp = card.querySelector(".outname");

  btn.addEventListener("click", () => {
    btn.disabled = true;
    btn.textContent = "生成中…";
    statusEl.className = "status";
    statusEl.textContent = "開始しています…";
    const body = new URLSearchParams();
    if (fastCb.checked) body.set("fast", "1");
    if (outnameInp.value.trim()) body.set("out_name", outnameInp.value.trim());
    fetch("/generate/" + slug, { method: "POST", body })
      .then(r => r.json())
      .then(data => {
        if (data.job_id) pollJob(data.job_id, statusEl, btn, "もう一度生成する");
        else { statusEl.className = "status err"; statusEl.textContent = data.error || "開始できませんでした"; btn.disabled = false; btn.textContent = "生成する"; }
      });
  });
});

document.getElementById("file-input").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  const clipStatus = document.getElementById("clip-status");
  file.text().then(text => {
    document.getElementById("paste-json").value = text;
    clipStatus.className = "status ok";
    clipStatus.textContent = file.name + " を読み込みました";
  }).catch(() => {
    clipStatus.className = "status err";
    clipStatus.textContent = "ファイルの読み込みに失敗しました";
  });
});

document.getElementById("clip-btn").addEventListener("click", async () => {
  const clipStatus = document.getElementById("clip-status");
  clipStatus.className = "status";
  clipStatus.textContent = "";
  try {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      throw new Error("unsupported");
    }
    const text = await navigator.clipboard.readText();
    if (!text || !text.trim()) {
      clipStatus.className = "status err";
      clipStatus.textContent = "クリップボードが空でした";
      return;
    }
    document.getElementById("paste-json").value = text;
    clipStatus.className = "status ok";
    clipStatus.textContent = "貼り付けました";
  } catch (e) {
    clipStatus.className = "status err";
    clipStatus.textContent = "自動で読み取れませんでした。テキストエリアを選んで手動で貼り付けて(Ctrl+V / Cmd+V)ください";
  }
});

document.getElementById("paste-btn").addEventListener("click", () => {
  const btn = document.getElementById("paste-btn");
  const statusEl = document.getElementById("paste-status");
  const text = document.getElementById("paste-json").value;
  const fast = document.getElementById("paste-fast").checked;
  const outName = document.getElementById("paste-outname").value.trim();
  btn.disabled = true;
  btn.textContent = "保存しています…";
  statusEl.className = "status";
  statusEl.textContent = "";
  const body = new URLSearchParams();
  body.set("json_text", text);
  if (fast) body.set("fast", "1");
  if (outName) body.set("out_name", outName);
  fetch("/generate_from_json", { method: "POST", body })
    .then(r => r.json())
    .then(data => {
      if (data.job_id) {
        btn.textContent = "生成中…";
        pollJob(data.job_id, statusEl, btn, "もう一度保存して生成する");
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
    data, status_code = _start_job(slug, fast, request.form.get("out_name", ""))
    return jsonify(data), status_code


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
    data, status_code = _start_job(slug, fast, request.form.get("out_name", ""))
    return jsonify(data), status_code


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job:
        return jsonify(job)

    # jobsに無い = 生成中にプロセスが再起動された可能性がある(メモリ/CPU制限
    # など)。job_id -> {slug, file_name} の対応表(ファイル)が残っていれば、
    # 出力ファイルの有無から状況を推測して分かりやすいメッセージを返す。
    rec = _load_jobs_state().get(job_id)
    if rec:
        file_name = rec.get("file_name") or f"{rec['slug']}.mp4"
        if (OUTPUT_DIR / file_name).exists():
            return jsonify({"status": "done", "slug": rec["slug"], "file_name": file_name, "log": "", "error": ""})
        return jsonify({
            "status": "error", "slug": rec["slug"], "file_name": file_name, "log": "",
            "error": (
                "生成の途中でサーバーが再起動された可能性があります"
                "(メモリ・CPU制限によるものと考えられます)。"
                "お手数ですが、もう一度「低画質プレビュー(高速)」で試してみてください。"
            ),
        })
    return jsonify({"error": "not found"}), 404


@app.route("/download/<name>")
def download(name):
    path = OUTPUT_DIR / f"{name}.mp4"
    if not path.exists():
        return "まだ生成されていません", 404
    return send_file(path, as_attachment=True, download_name=f"{name}.mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
