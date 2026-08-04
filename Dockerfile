FROM python:3.11-slim

# plotly + kaleido 在無頭環境輸出 PNG 圖表需要這些系統層級的 headless
# Chromium 相依套件（kaleido 本身有包一個內建的 Chromium 執行檔，但還是
# 需要作業系統提供這些共用函式庫）——這是 python:slim 基礎映像檔本來沒有的。
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    fonts-liberation \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements-dev.txt（含 requirements.txt + pytest）——啟動前會跑一次
# 完整測試套件才放行正式排程，呼應 run.sh 「測試沒過就中止」的慣例。
COPY requirements-dev.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .

CMD ["python", "cloud_scheduler.py"]
