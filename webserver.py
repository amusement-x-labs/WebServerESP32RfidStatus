import random
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional, Dict, Set, List, AsyncIterator
from fastapi import Query

from plyer import notification



# Setup of the notification about breaks
BREAK_SETTINGS = {
    "work_duration": 1800,  # 30 minutes of working activity (seconds)
    "break_interval": 180,  # 3 minutes betwee reminders (seconds)
    "active_notifications": False
}

# Storage of the active WebSocket connections
active_connections: List[WebSocket] = [] # for future usage

# Background task for time checkign and sending of the notifications
async def check_work_time():
    while True:
        if BREAK_SETTINGS["active_notifications"]:
            conn = sqlite3.connect('status_db.sqlite')
            cursor = conn.cursor()
            
            cursor.execute('''
            SELECT date, time, status FROM status_history
            WHERE status = 1
            ORDER BY date DESC, time DESC
            LIMIT 1
            ''')
            
            last_active = cursor.fetchone()
            conn.close()
            
            if last_active:
                last_active_dt = datetime.strptime(f"{last_active[0]} {last_active[1]}", "%Y-%m-%d %H:%M:%S")
                work_duration = (datetime.now() - last_active_dt).total_seconds()
                
                if work_duration > BREAK_SETTINGS["work_duration"]:
                    message = {
                        "type": "break_notification",
                        "message": "It's time to take a break!",
                        "work_duration": int(work_duration),
                        "timestamp": datetime.now().isoformat()
                    }
                    
                    # Notify via web sockets
                    for connection in active_connections:
                        try:
                            await connection.send_json(message)
                        except:
                            active_connections.remove(connection)

                    notification.notify(
                        title="Break reminder",
                        message="It's time to take a break! You have worked for     {} minutes.".format(int(work_duration // 60)),
                        app_name="Very Good App",
                        timeout=10
                    )

                    #print("Show toast")
        
        await asyncio.sleep(BREAK_SETTINGS["break_interval"])

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Launch bg task while app startup
    task = asyncio.create_task(check_work_time())
    yield
    # Stop the task when app closing
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)
security = HTTPBearer()

# SQLite initialization
def init_db():
    conn = sqlite3.connect('status_db.sqlite')
    cursor = conn.cursor()
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        status INTEGER NOT NULL
    )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# Users DB
USERS_DB = {
    "admin": {
        "password": "password123",
        "tokens": set(),
        "role": "admin"  # can everything
    },
    "monitor": {
        "password": "readonly456",
        "tokens": set(),
        "role": "viewer"  # only reading
    }
}

html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Break Control</title>
</head>
<body>
    <h1>Break Control will be done later</h1>
</body>
</html>
"""

class LoginRequest(BaseModel):
    username: str
    password: str

class StatusChangeRequest(BaseModel):
    status: bool

class StatusHistoryRequest(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    page: int = 1
    per_page: int = 100

def get_current_user(token: str = Depends(security)):
    credentials = token.credentials
    for username, user_data in USERS_DB.items():
        if credentials in user_data["tokens"]:
            return {
                "username": username,
                "role": user_data["role"]
            }
    raise HTTPException(status_code=401, detail="Invalid or expired token")

def check_admin_permissions(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")

def validate_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False

@app.get("/", response_class=HTMLResponse)
async def get_html():
    return HTMLResponse(content=html_content, status_code=200)

@app.post("/login")
async def login(login_data: LoginRequest):
    user_data = USERS_DB.get(login_data.username)
    if user_data and login_data.password == user_data["password"]:
        user_data["tokens"].clear()  # Release all previous tckens
        token = f"token-{random.randint(100000, 999999)}"
        user_data["tokens"].add(token)
        return {"token": token, "role": user_data["role"]}
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.post("/logout")
async def logout(user: dict = Depends(get_current_user)):
    username = user["username"]
    USERS_DB[username]["tokens"].clear()
    return {"message": "Logged out successfully"}

@app.websocket("/ws/notifications")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.remove(websocket)

@app.post("/statusChanged")
async def status_changed(
    status_data: StatusChangeRequest,
    user: dict = Depends(check_admin_permissions)  # Only for admin
):
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M:%S")
    
    conn = sqlite3.connect('status_db.sqlite')
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO status_history (date, time, status)
    VALUES (?, ?, ?)
    ''', (current_date, current_time, int(status_data.status)))
    
    conn.commit()
    conn.close()
    
    # Update notification status
    BREAK_SETTINGS["active_notifications"] = status_data.status
    
    #print(f"Status changed to {status_data.status} at {current_date} {current_time}")
    return {
        "message": "Status updated successfully",
        "new_status": status_data.status,
        "date": current_date,
        "time": current_time
    }

@app.get("/statusHistory")
async def get_status_history(
    start_date: Optional[str] = Query(None, description="Start date in YYYY-MM-DD format"),
    end_date: Optional[str] = Query(None, description="End date in YYYY-MM-DD format"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(100, ge=1, le=500, description="Items per page (max 500)"),
    user: dict = Depends(get_current_user) 
):
    now = datetime.now()
    default_end_date = now.strftime("%Y-%m-%d")
    default_start_date = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    
    start_date = start_date if start_date else default_start_date
    end_date = end_date if end_date else default_end_date
    
    if not validate_date(start_date) or not validate_date(end_date):
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    
    if datetime.strptime(start_date, "%Y-%m-%d") > datetime.strptime(end_date, "%Y-%m-%d"):
        raise HTTPException(status_code=400, detail="Start date must be before end date")
    
    delta = datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")
    if delta.days > 90:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 3 months")
    
    page = max(1, page)
    per_page = max(1, min(500, per_page))
    offset = (page - 1) * per_page
    
    conn = sqlite3.connect('status_db.sqlite')
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT COUNT(*) FROM status_history
    WHERE date BETWEEN ? AND ?
    ''', (start_date, end_date))
    total_records = cursor.fetchone()[0]
    
    cursor.execute('''
    SELECT date, time, status FROM status_history
    WHERE date BETWEEN ? AND ?
    ORDER BY date DESC, time DESC
    LIMIT ? OFFSET ?
    ''', (start_date, end_date, per_page, offset))
    
    history = [{
        "date": row[0],
        "time": row[1],
        "status": bool(row[2])
    } for row in cursor.fetchall()]
    
    conn.close()
    
    total_pages = (total_records + per_page - 1) // per_page
    
    return {
        "data": history,
        "pagination": {
            "total_records": total_records,
            "total_pages": total_pages,
            "current_page": page,
            "per_page": per_page,
            "has_next": page < total_pages,
            "has_prev": page > 1
        },
        "date_range": {
            "start_date": start_date,
            "end_date": end_date
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)