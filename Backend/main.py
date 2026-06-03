# main.py
import uvicorn
import json
import sqlite3
import os
import re
import uuid # 用來產生唯一檔名
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, status, Header, File, UploadFile,Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles # 用來提供靜態檔案存取
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from passlib.context import CryptContext
from jose import JWTError, jwt
from dotenv import load_dotenv

DB_NAME = "Database.db"

# --- JWT 設定 ---
load_dotenv()
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# --- 定義檔案上傳目錄、最大檔案大小、最大訊息長度 ---
UPLOAD_DIR = "static/uploads"
MAX_FILE_SIZE = 5 * 1024 * 1024 # 5MB
MAX_MSG_LENGTH = 500
os.makedirs(UPLOAD_DIR, exist_ok=True)

# [新增] 密碼雜湊器 (用來把密碼加密，不要存明碼！)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def init_db():
    """初始化資料庫"""

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # 1. 先開啟 WAL 模式
    c.execute("PRAGMA journal_mode=WAL;")

    # 2. [修正] 先建立資料表 (把所有需要的欄位一次建好)
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  nickname TEXT,
                  message TEXT,
                  msg_type TEXT,
                  timestamp TEXT,
                  filename TEXT,
                  is_deleted INTEGER DEFAULT 0)''') # <--- 直接加在這裡
    
    # [新增] 建立使用者表 (username 是主鍵，不可重複)
    # 注意：password_hash 存的是加密後的字串，不是明碼
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (username TEXT PRIMARY KEY,
                  password_hash TEXT)''')

    conn.commit()
    conn.close()

# --- 認證相關函式 ---
# [新增] 驗證密碼是否正確
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

# [新增] 將密碼加密
def get_password_hash(password):
    return pwd_context.hash(password)

# [新增] 產生 JWT Token
def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    
    # payload 裡面放入過期時間 (exp)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# [新增] 從 Token 解析出使用者 (用於 WebSocket 驗證)
def get_current_user_from_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
        return username
    except JWTError:
        return None

# [新增] 定義註冊/登入的資料格式
class UserAuth(BaseModel):
    username: str
    password: str

# [新增] 專門給註冊用的模型，多一個確認密碼欄位
class UserRegister(BaseModel):
    username: str
    password: str
    confirm_password: str

# [新增] 更改密碼用的資料模型
class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
    confirm_new_password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    username: str

def save_message(nickname, message, timestamp, msg_type="text", filename=None):
    """儲存訊息 (支援文字與圖片)"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # [修改] 插入時多存一個 msg_type
    c.execute("INSERT INTO messages (nickname, message, msg_type, timestamp, filename) VALUES (?, ?, ?, ?, ?)",
              (nickname, message, msg_type, timestamp, filename))
    message_id = c.lastrowid 
    conn.commit()
    conn.close()
    return message_id  # <== 回傳給上層

def get_recent_messages(limit=300, skip=0):
    """取得最近的歷史訊息"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # [修改] SQL 語法加入 OFFSET
    # 意思：抓最新的資料，但是跳過前 skip 筆，再抓 limit 筆
    c.execute("""
    SELECT nickname, message, msg_type, timestamp, id, is_deleted, filename
    FROM messages
    WHERE is_deleted = 0
    ORDER BY id DESC
    LIMIT ? OFFSET ?
    """, (limit, skip))

    rows = c.fetchall()
    conn.close()
    
    # 將資料轉為字典格式
    history = []
    for row in rows:
        msg_data = {
            "nickname": row[0],
            "type": row[2],
            "time": row[3],
            "id": row[4],
            "is_deleted": bool(row[5]), # [修正] index 5 才是 is_deleted
            "filename": row[6] if row[6] else None # [修正] index 6 才是 filename
        }
        
        # [修正重點] 這裡要同時允許 'text' 和 'system' 顯示 message 內容
        if row[2] in ["text", "system"]:
            msg_data["message"] = row[1]

        # 處理多媒體類型
        elif row[2] in ["image", "file", "video"]:
            msg_data["imageData"] = row[1] # 資料庫設計時把 URL 存在 message 欄位
            
            # 確保檔案和影片有預設檔名
            if row[2] == "file" and not msg_data["filename"]:
                msg_data["filename"] = "附件"
            elif row[2] == "video" and not msg_data["filename"]:
                msg_data["filename"] = "影片"
            
        history.append(msg_data)

    return history[::-1]

# 伺服器啟動時，初始化資料庫
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # [新增] 啟動背景寫入任務
    writer_task = asyncio.create_task(db_writer_worker())
    yield

class ConnectionManager:
    """管理 WebSocket 連線的類別"""
    def __init__(self):
        # 儲存活躍連線 (WebSocket: 暱稱)
        self.active_connections: Dict[WebSocket, str] = {}

    def get_member_list(self) -> List[str]:
        """取得所有成員的暱稱列表"""
        return list(set(self.active_connections.values()))

    # [修改] 回傳值型態註解改為 bool (True=是取代舊連線, False=是新連線)
    async def connect(self, websocket: WebSocket, nickname: str) -> bool:
        """接受一個新的 WebSocket 連線"""
        await websocket.accept()

        # [優化] 一行程式碼找出舊連線 (如果沒找到就回傳 None)
        existing_socket = next((ws for ws, user in self.active_connections.items() if user == nickname), None)
        
        if existing_socket:
            # 1. 先從清單刪除 (確保 disconnect 不會廣播離開)
            del self.active_connections[existing_socket]
            
            # 2. 關閉舊連線
            try:
                await existing_socket.close(code=4001, reason="Duplicate login")
            except Exception:
                pass # 忽略錯誤
            
            # 3. 加入新連線
            self.active_connections[websocket] = nickname
            return True # 回傳 True: 代表是「取代」舊連線
            
        # 如果沒有舊連線
        self.active_connections[websocket] = nickname
        return False # 回傳 False: 代表是「全新」連線

    def disconnect(self, websocket: WebSocket) -> str:
        """斷開一個 WebSocket 連線"""
        # [修改] 使用 pop 嘗試移除，如果 key 不存在 (代表已經在 connect 被踢掉了)，回傳 None
        return self.active_connections.pop(websocket, None)

    async def broadcast(self, payload: dict):
        """
        廣播 JSON 訊息給所有已連線的 WebSocket
        payload 是一個字典，我們會將它轉換為 JSON 字串
        """
        message_str = json.dumps(payload)  # 將字典轉為 JSON 字串
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message_str)
            except Exception:
                pass

# 實例化連線管理器
manager = ConnectionManager()

# [新增] 全域訊息佇列
message_queue = asyncio.Queue()

# [新增] 背景工兵：專門負責把佇列裡的訊息寫入資料庫
async def db_writer_worker():
    while True:
        # 從佇列拿出一個任務 (如果沒任務，這裡會自動等待，不佔資源)
        task = await message_queue.get()
        
        nickname, message, timestamp, msg_type, filename = task
        
        # 這裡執行原本的寫入邏輯 (使用 run_in_threadpool 避免卡住工兵)
        try:
            message_id = await run_in_threadpool(save_message, nickname, message, timestamp, msg_type, filename)
        except Exception as e:
            print(f"背景寫入失敗: {e}")
        
        # 標記任務完成
        message_queue.task_done()

# ... (FastAPI 實例化) ...
app = FastAPI(lifespan=lifespan)

# 掛載靜態檔案路徑 (這行一定要加，讓前端讀得到圖片)
app.mount("/static/uploads", StaticFiles(directory="static/uploads"), name="uploads")

# --- CORS 設定 ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 在開發階段，允許所有來源連線 (生產環境建議指定特定網域)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# [新增] 取得所有已註冊的使用者清單 (供前端計算離線成員用)
@app.get("/users")
async def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 只選取 username 欄位
    c.execute("SELECT username FROM users")
    rows = c.fetchall()
    conn.close()
    
    # rows 的格式會是 [('alice',), ('bob',), ...]
    # 我們要把它轉成單純的 list: ['alice', 'bob', ...]
    all_users = [row[0] for row in rows]
    
    return all_users

# [新增] 載入更多歷史訊息 API
@app.get("/history/more")
async def get_more_history(skip: int = 0, limit: int = 50):
    # 直接呼叫上面改好的函式
    return get_recent_messages(limit, skip)

# [修改] 註冊 API：改用 UserRegister 模型並加入驗證邏輯
@app.post("/register")
async def register(user: UserRegister):
    # 1. 檢查兩次密碼是否輸入一致
    if user.password != user.confirm_password:
        raise HTTPException(status_code=400, detail="兩次輸入的密碼不一致")

    # 2. 檢查帳號長度
    if len(user.username) < 2 or len(user.username) > 16:
        raise HTTPException(status_code=400, detail="帳號長度必須在 2 到 16 個字之間")

    # 3. 檢查密碼長度
    if len(user.password) < 8:
        raise HTTPException(status_code=400, detail="密碼長度至少需要 8 個字元")
    if len(user.password) > 72:
        # 只有真的踩到這條線的人才會看到這個訊息
        raise HTTPException(status_code=400, detail="密碼長度過長 (系統限制最多 72 字元)")

    # 4. 密碼複雜度 (Regex 檢查)
    if not re.search(r"[A-Za-z]", user.password) or not re.search(r"\d", user.password):
        raise HTTPException(status_code=400, detail="密碼必須包含至少一個英文字母與一個數字")

    # 5. 資料庫檢查
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 3. 檢查帳號是否已存在
    c.execute("SELECT username FROM users WHERE username = ?", (user.username,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="此帳號已被註冊")
    
    # 4. 將密碼加密後存入資料庫
    hashed_password = get_password_hash(user.password)
    c.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", 
              (user.username, hashed_password))
    
    conn.commit()
    conn.close()
    return {"message": "User created successfully"}

# [新增] 登入 API
@app.post("/token", response_model=Token)
async def login_for_access_token(user_data: UserAuth):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 找使用者
    c.execute("SELECT username, password_hash FROM users WHERE username = ?", (user_data.username,))
    row = c.fetchone()
    conn.close()
    
    # 驗證帳號存在 且 密碼正確
    if not row or not verify_password(user_data.password, row[1]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 登入成功，發放 Token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user_data.username}, expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "username": user_data.username
    }

# [新增] 更改密碼 API
@app.post("/change-password")
async def change_password(
    req: ChangePasswordRequest, 
    authorization: str = Header(None) # 從 Header 取得 Token
):
    # 1. 驗證 Token 是否存在
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登入或 Token 無效")
    
    token = authorization.split(" ")[1]
    username = get_current_user_from_token(token)
    
    if not username:
        raise HTTPException(status_code=401, detail="Token 無效或過期")

    # 2. 驗證新密碼格式 (與註冊時相同的邏輯)
    if req.new_password != req.confirm_new_password:
        raise HTTPException(status_code=400, detail="兩次新密碼輸入不一致")

    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密碼長度至少需要 8 個字元")
    
    if not re.search(r"[A-Za-z]", req.new_password) or not re.search(r"\d", req.new_password):
        raise HTTPException(status_code=400, detail="新密碼必須包含至少一個英文字母與一個數字")

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 3. 取得目前使用者資料
    c.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="使用者不存在")
    
    current_password_hash = row[0]

    # 4. 驗證舊密碼是否正確
    if not verify_password(req.old_password, current_password_hash):
        conn.close()
        raise HTTPException(status_code=400, detail="舊密碼錯誤")

    # 5. 更新密碼
    new_hashed_password = get_password_hash(req.new_password)
    c.execute("UPDATE users SET password_hash = ? WHERE username = ?", (new_hashed_password, username))
    conn.commit()
    conn.close()

    return {"message": "密碼修改成功"}

# [新增] 專門處理圖片上傳的 API
# 前端會用 Form Data (multipart/form-data) 傳送檔案到這裡
@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        # 允許的副檔名類型
        allowed_extensions = ["jpg", "jpeg", "png", "gif", "pdf", "doc", "docx", "zip", "rar","mp4", "webm", "heic"]

        # 取得副檔名
        filename_ext = file.filename.split(".")[-1].lower()
        if filename_ext not in allowed_extensions:
            raise HTTPException(status_code=400, detail="不支援的檔案類型")

        # [新增] 檢查檔案大小
        # UploadFile 是 SpooledTemporaryFile，我們可以移到檔案尾端看大小
        file.file.seek(0, 2) # 移到檔案最後面
        file_size = file.file.tell() # 取得目前位置 (即檔案大小)
        file.file.seek(0) # 記得移回檔案最前面，不然等下 read() 會讀不到東西！

        if file_size > MAX_FILE_SIZE:
            # 轉換成 MB 顯示錯誤訊息比較好讀
            size_in_mb = MAX_FILE_SIZE / (1024 * 1024)
            raise HTTPException(status_code=400, detail=f"檔案過大，限制為 {int(size_in_mb)}MB")

        # 產生亂碼檔名
        unique_filename = f"{uuid.uuid4()}.{filename_ext}"
        file_path = os.path.join(UPLOAD_DIR, unique_filename)

        # 儲存檔案
        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        return {"url": f"/static/uploads/{unique_filename}"}

    except HTTPException as he:
        raise he # 如果是我們自己拋出的 HTTP 錯誤，直接往外丟
    except Exception as e:
        print(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail="檔案上傳失敗")

# --- WebSocket 路由 (聊天室核心) ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(None)):
    # 注意：這裡將 nickname 改為接收 token

    if token is None:
        await websocket.close(code=4003, reason="Token missing")
        return
    
    # 1. 驗證 Token，取得使用者名稱
    username = get_current_user_from_token(token)
    
    if username is None:
        # Token 無效或過期
        await websocket.close(code=4003, reason="Invalid token")
        return

    # 2. 嘗試連線
    # [核心修改] 接收回傳值：is_replaced (是否為取代舊連線)
    is_replaced = await manager.connect(websocket, username)

    # 連線成功後，傳送歷史訊息
    history = get_recent_messages()
    # 我們定義一個新的類型 'history'
    await websocket.send_text(json.dumps({"type": "history", "messages": history}))

    # [核心修改] 只有在「不是」取代舊連線的情況下，才廣播加入訊息
    if not is_replaced:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 存入資料庫
        message_queue.put_nowait(("系統", f"{username} 加入了聊天室", timestamp, "system", None))
        
        # 廣播
        await manager.broadcast({"type": "system", "message": f"{username} 加入了聊天室"})
    # 無論是新連線還是取代舊連線，都廣播最新的成員名單
    await manager.broadcast({"type": "member_list_update", "members": manager.get_member_list()})

    try:
        while True:
            data = await websocket.receive_text()

            # --- [新增] 訊息長度檢查 ---
            if len(data) > MAX_MSG_LENGTH:
                # 選擇性：可以回傳一個系統訊息警告使用者
                warning_msg = json.dumps({
                    "type": "system", 
                    "message": f"訊息過長 (超過 {MAX_MSG_LENGTH} 字)，傳送失敗。"
                })
                await websocket.send_text(warning_msg)
                continue # 跳過這次迴圈，不處理這則訊息
            # --------------------------
            try:
                parsed = json.loads(data)

                # [修改] 加入型別檢查：如果解析出來不是字典 (例如是 int, list, string)，則視為格式不符
                if not isinstance(parsed, dict):
                    raise ValueError("Parsed JSON is not a dictionary")

                msg_type = parsed.get("type")
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if msg_type == "image":
                    # [修改重點在此]
                    # 現在前端傳過來的 imageData 不再是巨大的 Base64，
                    # 而是剛剛從 /upload API 拿到的 "網址" (例如 /static/uploads/xxx.jpg)
                    image_url = parsed.get("imageData")

                    if image_url:
                        # 丟進佇列 (Tuple 格式要跟 worker 對應)
                        message_id = save_message(username, image_url, timestamp, "image", None)
                        
                        # 2. 廣播給所有人 (包含傳送者)
                        await manager.broadcast({
                            "type": "image",
                            "nickname": username,
                            "imageData": image_url, # 這裡廣播網址
                            "time": timestamp,
                            "id": message_id
                        })
                elif msg_type == "file":
                    file_url = parsed.get("imageData")  # 雖然是檔案，但欄位仍用 imageData
                    filename = parsed.get("filename", "附件")

                    if file_url:
                        message_id = save_message(username, file_url, timestamp, "file", filename)
                        await manager.broadcast({
                            "type": "file",
                            "nickname": username,
                            "imageData": file_url,
                            "filename": filename,
                            "time": timestamp,
                            "id": message_id
                        })
                elif msg_type == "video":
                    video_url = parsed.get("imageData")
                    filename = parsed.get("filename", "影片")

                    if video_url:
                        message_id = save_message(username, video_url, timestamp, "video", filename)
                        await manager.broadcast({
                            "type": "video",
                            "nickname": username,
                            "imageData": video_url,
                            "filename": filename,
                            "time": timestamp,
                            "id": message_id
                        })

                else:
                    # 一般文字訊息
                    message_id = save_message(username, data, timestamp, "text", None)
                    await manager.broadcast({
                        "type": "chat",
                        "nickname": username,
                        "message": data,
                        "time": timestamp,
                        "id": message_id  # 加入 id
                    })

            # [修改] 除了 JSONDecodeError，也要捕捉 ValueError (我們剛剛手動引發的) 或 AttributeError
            except (json.JSONDecodeError, ValueError, AttributeError) as e:
                # 錯誤處理
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # 這裡將 data (原始字串) 當作純文字訊息儲存
                message_id = save_message(username, data, timestamp, "text", None)
                await manager.broadcast({
                    "type": "chat",
                    "nickname": username,
                    "message": data,
                    "time": timestamp,
                    "id": message_id  # 加入 id
                })
            
    except WebSocketDisconnect:
        nickname_left = manager.disconnect(websocket) # 斷線處理

        # 只有當 disconnect 回傳有值時，才代表是「使用者自己斷線/關閉網頁」
        # 如果回傳 None，代表它是「被踢掉的舊連線」，我們就不廣播離開訊息
        if nickname_left:
            # 處理離開訊息
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 存入資料庫
            message_queue.put_nowait(("系統", f"{nickname_left} 離開了聊天室", timestamp, "system", None))

            # 廣播離開訊息
            await manager.broadcast({"type": "system", "message": f"{nickname_left} 離開了聊天室"})
            await manager.broadcast({"type": "member_list_update", "members": manager.get_member_list()})

@app.post("/delete-message")
async def delete_message(id: int, authorization: str = Header(None)):
    # 1. 取出使用者
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登入或 Token 無效")

    token = authorization.split(" ")[1]
    username = get_current_user_from_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Token 無效或過期")

    # 2. 查詢此訊息是否存在 + 是否屬於該使用者
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT nickname FROM messages WHERE id = ?", (id,))
    row = c.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="找不到該訊息")

    if row[0] != username:
        conn.close()
        raise HTTPException(status_code=403, detail="只能刪除自己的訊息")

    # 3. 更新 is_deleted 為 1
    c.execute("UPDATE messages SET is_deleted = 1 WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    await manager.broadcast({
        "type": "delete",
        "id": id
    })
    return {"message": "刪除成功"}

# 2. Nuxt 的靜態資源 (_nuxt 資料夾)
# 我們等下會在 Dockerfile 裡把 Nuxt 打包好的檔案複製到 /app/frontend
# Nuxt 3 的資源通常放在 _nuxt 目錄下
if os.path.exists("frontend/_nuxt"):
    app.mount("/_nuxt", StaticFiles(directory="frontend/_nuxt"), name="nuxt_assets")

# 3. 處理根目錄與 SPA 路由 (Catch-all route)
# 這樣使用者重新整理網頁時，才不會變成 404，而是回到 Nuxt 的 index.html
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    # 如果是 API 請求，或是上面的 static/uploads，不應該進來這裡
    # 但因為 FastAPI 路由優先順序，定義在上面的 API 會先被匹配，所以這裡只會抓到「剩下的」
    
    # 檢查是否請求了根目錄下的靜態檔 (例如 favicon.ico, robots.txt)
    file_path = os.path.join("frontend", full_path)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)
        
    # 其他所有路徑 (例如 /chat, /login) 都回傳 index.html
    return FileResponse("frontend/index.html")

# 允許在 Python 腳本中直接執行
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)