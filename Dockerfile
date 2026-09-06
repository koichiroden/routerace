# Render (Docker Runtime) 向けの最小構成。
# ffmpeg と日本語フォント(Noto Sans CJK)を含める。
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 例: configs/shinjuku_fujisawa.json を生成する場合
# (Renderで動かす場合はWebサーバー経由でジョブを起動する形に置き換えてください)
CMD ["python3", "-m", "race_video.cli", "configs/shinjuku_fujisawa.json"]
