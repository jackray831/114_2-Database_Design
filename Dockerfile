# --- 第一階段：建置 Nuxt 前端 ---
FROM node:alpine as frontend-build

WORKDIR /app-frontend

# 複製 package.json
COPY Frontend/package*.json ./
RUN npm install

# 複製所有前端程式碼
COPY Frontend/ .

# 執行 Nuxt 靜態生成 (這會產生 .output/public 資料夾)
RUN npm run generate


# --- 第二階段：建置 Python 後端 ---
FROM python:3.12-slim

WORKDIR /app

# 設定 Python 不緩衝輸出 (方便看 Log)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# [新增] 設定時區環境變數 (這行最重要！)
ENV TZ=Asia/Taipei

# [修改] 安裝 sqlite3 以及 tzdata (時區資料)
RUN apt-get update && \
    apt-get install -y sqlite3 tzdata && \
    rm -rf /var/lib/apt/lists/*

# 複製 requirements.txt 並安裝
COPY Backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 1. 建立後端需要的資料夾
# 為了避免找不到檔案，我們先建立 uploads 和 frontend 資料夾
RUN mkdir -p static/uploads && mkdir -p frontend

# 2. 把 Nuxt 打包好的檔案 (.output/public) 複製到 /app/frontend
COPY --from=frontend-build /app-frontend/.output/public ./frontend

# 3. 複製後端程式碼
COPY Backend/ .
# 如果你有 .env.example 或其他 config 檔也要複製
# COPY .env .  <-- 注意：Render 上通常是用環境變數設定，不建議複製真實的 .env

# Render 需要的 Port
EXPOSE 8000

# 啟動命令
CMD ["python", "main.py"]