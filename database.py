import socket
import threading
import sqlite3
import json
from typing import Optional

from utils import ok, err, send_json, recv_json

HOST = "140.113.17.11"
PORT = 19805
DB_PATH = "np_hw.db"

# 修正 1 & 2: 更新 Schema，加入 file_path, properties, user_plugins, relations
SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username  TEXT UNIQUE NOT NULL,
  password  TEXT NOT NULL,
  status    TEXT NOT NULL DEFAULT 'OFFLINE',
  properties TEXT DEFAULT '{}',  -- [Extensibility] JSON 欄位 (設定/背包)
  last_login TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

CREATE TABLE IF NOT EXISTS developers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username  TEXT UNIQUE NOT NULL,
  password  TEXT NOT NULL,
  status    TEXT NOT NULL DEFAULT 'OFFLINE',
  last_login TIMESTAMP
);

CREATE TABLE IF NOT EXISTS games(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gamename TEXT UNIQUE NOT NULL,
  owner  TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'UNLOADED',
  latest TEXT NOT NULL DEFAULT 'v0.0.0',
  file_path TEXT,  -- [Architecture] 紀錄實體檔案路徑
  FOREIGN KEY(owner) REFERENCES developers(username)
);

-- [Extensibility] Plugin 系統
CREATE TABLE IF NOT EXISTS user_plugins(
  username TEXT NOT NULL,
  plugin_name TEXT NOT NULL,
  is_enabled INTEGER DEFAULT 1,
  PRIMARY KEY (username, plugin_name),
  FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE
);

-- [Extensibility] 社交系統 (好友/黑名單)
CREATE TABLE IF NOT EXISTS relations(
  user_a TEXT NOT NULL,
  user_b TEXT NOT NULL,
  type TEXT NOT NULL, -- 'FRIEND', 'BLOCK', 'REQUEST'
  created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (user_a, user_b),
  FOREIGN KEY(user_a) REFERENCES users(username) ON DELETE CASCADE,
  FOREIGN KEY(user_b) REFERENCES users(username) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS downloads(
  username TEXT NOT NULL,
  gamename TEXT NOT NULL,
  version  TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT (datetime('now','localtime')),
  PRIMARY KEY(username, gamename),
  FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE,
  FOREIGN KEY(gamename) REFERENCES games(gamename) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ratings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gamename TEXT NOT NULL,
  username TEXT NOT NULL,
  score INTEGER NOT NULL,
  comment TEXT,
  created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
  FOREIGN KEY(gamename) REFERENCES games(gamename) ON DELETE CASCADE,
  FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rooms(
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  public INTEGER NOT NULL DEFAULT 1,
  open INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
  FOREIGN KEY(owner) REFERENCES users(username)
);

CREATE TABLE IF NOT EXISTS invites(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id TEXT NOT NULL,
  from_user TEXT NOT NULL,
  to_user   TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
  FOREIGN KEY(room_id)   REFERENCES rooms(id)        ON DELETE CASCADE,
  FOREIGN KEY(from_user) REFERENCES users(username)  ON DELETE CASCADE,
  FOREIGN KEY(to_user)   REFERENCES users(username)  ON DELETE CASCADE
);
"""

def _safe_exec(cur, sql, args: Optional[tuple] = None):
    try:
        if args:
            cur.execute(sql, args)
        else:
            cur.execute(sql)
    except Exception:
        pass

def reset_runtime(db):
    """重置執行期間的暫態資料，並同步清除記憶體快取"""
    conn = db.conn
    cur = conn.cursor()
    _safe_exec(cur, "PRAGMA foreign_keys = ON;")
    _safe_exec(cur, "DELETE FROM rooms;")
    _safe_exec(cur, "DELETE FROM invites;")
    _safe_exec(cur, "DELETE FROM user_plugins;") # 視需求是否重置
    # 將所有人下線
    _safe_exec(cur, "UPDATE users SET status='OFFLINE', last_login=NULL;")
    conn.commit()

    # [State Consistency] 清空記憶體快取
    with db.lock:
        db.online_cache.clear()

def reset_dev_runtime(db):
    conn = db.conn
    cur = conn.cursor()
    _safe_exec(cur, "PRAGMA foreign_keys = ON;")
    _safe_exec(cur, "UPDATE developers SET status='OFFLINE', last_login=NULL;")
    conn.commit()

class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        
        # [State Consistency] Memory Source of Truth
        # 用於解決 DB 狀態與實際連線不一致的問題
        self.online_cache = set() 
        self.dev_online_cache = set()

        with self.lock, self.conn:
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self.conn.executescript(SCHEMA_SQL)

    # ================= User Auth & State =================
    
    def is_online(self, username: str) -> bool:
        # [State Consistency] 直接查記憶體，不再查 DB
        with self.lock:
            return username in self.online_cache

    def register(self, username: str, password: str):
        if not username or not password:
            return False, "invalid args"
        try:
            with self.lock, self.conn:
                self.conn.execute(
                    "INSERT INTO users (username, password, status) VALUES(?, ?, 'OFFLINE')",
                    (username, password),
                )
                return True, "registered"
        except sqlite3.IntegrityError:
            return False, "user exists"
        except Exception:
            return False, "db error"

    def login(self, username: str, password: str):
        if not username or not password:
            return False, "invalid args"
        with self.lock:
            cur = self.conn.execute(
                "SELECT id FROM users WHERE username=? AND password=?",
                (username, password),
            )
            row = cur.fetchone()
            if not row:
                return False, "bad credential"
            
            # [State Consistency] 更新記憶體與 DB
            self.online_cache.add(username)
            with self.conn:
                self.conn.execute(
                    "UPDATE users SET status='ONLINE', last_login=datetime('now','localtime') WHERE username=?",
                    (username,),
                )
        return True, "login"

    def logout(self, username: str):
        # 新增 Logout 方法，供 Explicit Quit 使用
        with self.lock:
            if username in self.online_cache:
                self.online_cache.remove(username)
                with self.conn:
                    self.conn.execute("UPDATE users SET status='OFFLINE' WHERE username=?", (username,))
                return True, "logged out"
            return False, "not online"

    def show_status(self, username: str):
        if not username: return False, "invalid args"
        with self.lock:
            # 優先回傳記憶體中的狀態
            is_on = username in self.online_cache
            status_str = "ONLINE" if is_on else "OFFLINE"
            
            cur = self.conn.execute("SELECT username, last_login, properties FROM users WHERE username=?", (username,))
            row = cur.fetchone()
            if not row: return False, "no such user"
            
            info = {
                "username": row["username"],
                "status": status_str,
                "last_login": row["last_login"],
                "properties": json.loads(row["properties"] or '{}') # [Extensibility]
            }
        return True, info

    def who(self, only_online: bool):
        # 若只查線上，直接回傳 cache 內容，效能更好
        if only_online:
            with self.lock:
                return [{"username": u, "status": "ONLINE"} for u in sorted(list(self.online_cache))]
        
        sql = "SELECT username, status FROM users ORDER BY username ASC"
        with self.lock:
            rows = self.conn.execute(sql).fetchall()
        
        # 修正：即使 DB 寫 OFFLINE，若在 Cache 中也視為 ONLINE (雖理論上同步，但以 Cache 為準)
        res = []
        for r in rows:
            u = r["username"]
            real_status = "ONLINE" if u in self.online_cache else "OFFLINE"
            res.append({"username": u, "status": real_status})
        return res

    # ================= Extensibility: Social & Plugins =================
    
    def add_friend(self, user_a, user_b):
        if user_a == user_b: return False, "cannot add self"
        try:
            with self.lock, self.conn:
                # 雙向關係或單向視需求而定，這裡示範建立單向好友請求
                self.conn.execute(
                    "INSERT OR IGNORE INTO relations (user_a, user_b, type) VALUES (?, ?, 'FRIEND')",
                    (user_a, user_b)
                )
            return True, "friend added"
        except Exception as e:
            return False, str(e)

    def list_friends(self, username):
        with self.lock:
            rows = self.conn.execute(
                "SELECT user_b FROM relations WHERE user_a=? AND type='FRIEND'", (username,)
            ).fetchall()
        return [r["user_b"] for r in rows]

    def set_plugin_status(self, username, plugin_name, enabled: bool):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO user_plugins (username, plugin_name, is_enabled) VALUES(?,?,?) "
                "ON CONFLICT(username, plugin_name) DO UPDATE SET is_enabled=excluded.is_enabled",
                (username, plugin_name, 1 if enabled else 0)
            )
        return True, "plugin updated"

    # ================= Developer =================
    
    def dev_login(self, username: str, password: str):
        # 類似 User Login，使用 dev_online_cache
        with self.lock:
            cur = self.conn.execute("SELECT id FROM developers WHERE username=? AND password=?", (username, password))
            if not cur.fetchone(): return False, "bad credential"
            
            self.dev_online_cache.add(username)
            with self.conn:
                self.conn.execute("UPDATE developers SET status='ONLINE' WHERE username=?", (username,))
        return True, "dev login"

    def dev_is_online(self, username: str) -> bool:
        with self.lock:
            return username in self.dev_online_cache
            
    def dev_logout(self, username: str):
        with self.lock:
            if username in self.dev_online_cache:
                self.dev_online_cache.remove(username)
                with self.conn:
                    self.conn.execute("UPDATE developers SET status='OFFLINE' WHERE username=?", (username,))
                return True, "dev logout"
            return False, "not online"
            
    def dev_register(self, username, password):
        # 省略，邏輯同 user register，僅表名不同
        try:
            with self.lock, self.conn:
                self.conn.execute("INSERT INTO developers (username, password) VALUES(?,?)", (username, password))
                return True, "dev registered"
        except: return False, "error"

    def dev_list_games(self, owner: str):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, gamename, status, latest, file_path FROM games WHERE owner=? ORDER BY gamename ASC",
                (owner,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "gamename": r["gamename"],
                "status": r["status"],
                "latest": r["latest"],
                "file_path": r["file_path"],
            }
            for r in rows
        ]

    def dev_create_game(self, gamename: str, owner: str, file_path: str = None):
        if not gamename or not owner: return False, "invalid args"
        try:
            with self.lock, self.conn:
                self.conn.execute(
                    "INSERT INTO games (gamename, owner, file_path) VALUES(?, ?, ?)",
                    (gamename, owner, file_path),
                )
            return True, "game created"
        except sqlite3.IntegrityError: return False, "game exists"
        except Exception: return False, "db error"
        
    def dev_update_game_path(self, owner, gamename, file_path):
        # [Architecture] 更新檔案路徑的專用方法
        if not self.dev_is_owner(owner, gamename): return False, "not your game"
        with self.lock, self.conn:
            self.conn.execute("UPDATE games SET file_path=? WHERE gamename=?", (file_path, gamename))
        return True, "path updated"
        
    def dev_is_owner(self, owner: str, gamename: str) -> bool:
        with self.lock:
            cur = self.conn.execute("SELECT 1 FROM games WHERE gamename=? AND owner=?", (gamename, owner))
            return cur.fetchone() is not None

    def dev_update_game(self, owner, gamename, version):
        if not self.dev_is_owner(owner, gamename): return False, "not your game"
        with self.lock, self.conn:
            self.conn.execute("UPDATE games SET latest=?, status='UPDATED' WHERE gamename=?", (version, gamename))
        return True, "updated"

    def dev_set_game_status(self, owner, gamename, status):
        if not self.dev_is_owner(owner, gamename): return False, "not your game"
        with self.lock, self.conn:
            self.conn.execute("UPDATE games SET status=? WHERE gamename=?", (status, gamename))
        return True, "status changed"

    # ================= Lobby / Room =================
    # (保留原有的 create_room, list_rooms, join/leave 邏輯，
    # 但要注意：這些通常只操作 DB，不涉及 Online 狀態檢查，所以變動不大)
    
    def create_room(self, room_id, owner, public):
        try:
            with self.lock, self.conn:
                self.conn.execute("INSERT INTO rooms (id, owner, public) VALUES(?,?,?)", (room_id, owner, 1 if public else 0))
            return True, "created"
        except: return False, "error"
        
    def list_rooms(self, only_public=False):
        sql = "SELECT id, owner, public, open FROM rooms"
        if only_public: sql += " WHERE public=1"
        with self.lock:
            rows = self.conn.execute(sql).fetchall()
        return [{"id":r["id"], "owner":r["owner"], "public":bool(r["public"]), "open":bool(r["open"])} for r in rows]

    def close_room(self, room_id):
        with self.lock, self.conn:
            self.conn.execute("UPDATE rooms SET open=0 WHERE id=?", (room_id,))
        return True

    def delete_room(self, room_id):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
        return True

    # ================= Store / Downloads =================
    def list_store_games(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT gamename, owner, status, latest, file_path FROM games WHERE status='PUBLISHED' ORDER BY gamename ASC"
            ).fetchall()
        return [
            {
                "gamename": r["gamename"],
                "owner": r["owner"],
                "status": r["status"],
                "latest": r["latest"],
                "file_path": r["file_path"],
            }
            for r in rows
        ]
        
    def download_game(self, username, gamename):
        # 只是紀錄下載行為，不負責傳檔
        # 要先查版本
        with self.lock:
            cur = self.conn.execute(
                "SELECT latest, status FROM games WHERE gamename=?", (gamename,)
            )
            row = cur.fetchone()
            if not row:
                return False, "no such game"
            if row["status"] != "PUBLISHED":
                return False, "game not published"
            ver = row["latest"]
            
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO downloads (username, gamename, version) VALUES(?,?,?) "
                "ON CONFLICT(username, gamename) DO UPDATE SET version=excluded.version, updated_at=datetime('now')",
                (username, gamename, ver)
            )
        return True, "recorded"

    def my_downloads(self, username):
        with self.lock:
            rows = self.conn.execute("SELECT gamename, version FROM downloads WHERE username=?", (username,)).fetchall()
        return [{"gamename":r["gamename"], "version":r["version"]} for r in rows]

    def rate_game(self, username, gamename, score, comment):
        # 檢查是否有下載
        with self.lock:
            if not self.conn.execute("SELECT 1 FROM downloads WHERE username=? AND gamename=?", (username, gamename)).fetchone():
                return False, "download first"
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO ratings (gamename, username, score, comment) VALUES(?,?,?,?)", (gamename, username, score, comment))
        return True, "rated"
        
    def list_ratings(self, gamename):
        with self.lock:
            rows = self.conn.execute("SELECT username, score, comment FROM ratings WHERE gamename=?", (gamename,)).fetchall()
        return [{"username":r["username"], "score":r["score"], "comment":r["comment"]} for r in rows]


db = DB(DB_PATH)

def handle_client(conn, addr):    
    try:
        while True:
            msg = recv_json(conn)
            if msg is None: break

            action = msg.get("action")
            role = msg.get("role", "user") # user or dev

            if role == "dev":
                # === Dev Actions ===
                if action == "dev_register":
                    okb, m = db.dev_register(msg.get("username"), msg.get("password"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "dev_login":
                    okb, m = db.dev_login(msg.get("username"), msg.get("password"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "dev_create_game":
                    # 支援 file_path
                    okb, m = db.dev_create_game(msg.get("gamename"), msg.get("owner"), msg.get("file_path"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "dev_update_game_path":
                    okb, m = db.dev_update_game_path(msg.get("owner"), msg.get("gamename"), msg.get("file_path"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "dev_update_game":
                    okb, m = db.dev_update_game(msg.get("owner"), msg.get("gamename"), msg.get("version"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "dev_set_game_status":
                    okb, m = db.dev_set_game_status(msg.get("owner"), msg.get("gamename"), msg.get("status"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "dev_list_games":
                    games = db.dev_list_games(msg.get("owner"))
                    send_json(conn, ok(games=games))
                elif action == "reset_dev_runtime":
                    reset_dev_runtime(db)
                    send_json(conn, ok("reset"))
                elif action == "quit": # Explicit Dev Logout
                    db.dev_logout(msg.get("username"))
                    send_json(conn, ok("bye"))
                else:
                    # 其他 Dev actions (update, set_status...) 省略，依此類推
                    send_json(conn, err("unknown dev action"))

            else:
                # === User Actions ===
                if action == "register":
                    okb, m = db.register(msg.get("username"), msg.get("password"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "login":
                    okb, m = db.login(msg.get("username"), msg.get("password"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "show_status":
                    okb, val = db.show_status(msg.get("username"))
                    send_json(conn, ok(val) if okb else err(val))
                elif action == "who_online":
                    send_json(conn, ok(users=db.who(True)))
                elif action == "quit": # Explicit User Logout
                    db.logout(msg.get("username"))
                    send_json(conn, ok("bye"))
                
                # ... 其他 Actions (create_room, download_game...) 直接呼叫 db 對應方法即可 ...
                # 這裡為了簡潔省略大量 elif，實作時請保留原有的 dispatch 邏輯
                elif action == "list_store_games":
                    send_json(conn, ok(games=db.list_store_games()))
                elif action == "download_game":
                    okb, m = db.download_game(msg.get("username"), msg.get("gamename"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "my_downloads":
                    send_json(conn, ok(downloads=db.my_downloads(msg.get("username"))))
                elif action == "rate_game":
                    okb, m = db.rate_game(msg.get("username"), msg.get("gamename"), msg.get("score"), msg.get("comment", ""))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "list_ratings":
                    send_json(conn, ok(ratings=db.list_ratings(msg.get("gamename"))))
                elif action == "create_room":
                    okb, m = db.create_room(msg.get("room_id"), msg.get("owner"), msg.get("public"))
                    send_json(conn, ok(m) if okb else err(m))
                elif action == "list_rooms":
                    send_json(conn, ok(rooms=db.list_rooms(msg.get("only_public"))))
                elif action == "reset_runtime":
                    reset_runtime(db)
                    send_json(conn, ok("reset"))
                elif action == "finish_game":
                    # 目前 lobby 端只需要不噴錯；可依需求把 summary 寫入 DB
                    send_json(conn, ok("finished"))
                else:
                    send_json(conn, err("unknown action"))

    except Exception as e:
        print(f"[DB] Error: {e}")
    finally:
        conn.close()

def run_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(64)
    print(f"[DB] Server listening on {HOST}:{PORT}")
    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[DB] Shutting down...")
    finally:
        srv.close()

if __name__ == "__main__":
    run_server()
