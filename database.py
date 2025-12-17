import socket
import threading
import sqlite3
from typing import Optional

from utils import ok, err, send_json, recv_json

HOST = "140.113.17.11"
PORT = 19800
DB_PATH = "np_hw.db"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username  TEXT UNIQUE NOT NULL,
  password  TEXT NOT NULL,
  status    TEXT NOT NULL DEFAULT 'OFFLINE',
  last_login TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_status   ON users(status);

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
  FOREIGN KEY(owner) REFERENCES developers(username)
);

-- 玩家下載紀錄（版本管理）
CREATE TABLE IF NOT EXISTS downloads(
  username TEXT NOT NULL,
  gamename TEXT NOT NULL,
  version  TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT (datetime('now','localtime')),
  PRIMARY KEY(username, gamename),
  FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE,
  FOREIGN KEY(gamename) REFERENCES games(gamename) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(username);
CREATE INDEX IF NOT EXISTS idx_downloads_game ON downloads(gamename);

-- 玩家評分與留言
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
CREATE INDEX IF NOT EXISTS idx_ratings_game ON ratings(gamename);

CREATE TABLE IF NOT EXISTS rooms(
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  public INTEGER NOT NULL DEFAULT 1,
  open INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
  FOREIGN KEY(owner) REFERENCES users(username)
);
CREATE INDEX IF NOT EXISTS idx_rooms_public ON rooms(public);
CREATE INDEX IF NOT EXISTS idx_rooms_open   ON rooms(open);

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
CREATE INDEX IF NOT EXISTS idx_invites_to   ON invites(to_user);
CREATE INDEX IF NOT EXISTS idx_invites_room ON invites(room_id);
"""

ACTIVE_USERS = {}
ACTIVE_LOCK = threading.Lock()


def _safe_exec(cur, sql, args: Optional[tuple] = None):
    try:
        if args:
            cur.execute(sql, args)
        else:
            cur.execute(sql)
    except Exception:
        pass

def reset_runtime(db):
    conn = db.conn
    cur = conn.cursor()
    _safe_exec(cur, "PRAGMA foreign_keys = ON;")
    # 清空對戰/配對相關的暫態資料
    _safe_exec(cur, "DELETE FROM rooms;")
    _safe_exec(cur, "DELETE FROM invites;")
    _safe_exec(cur, "DELETE FROM downloads;")
    _safe_exec(cur, "DELETE FROM ratings;")
    _safe_exec(cur, "DELETE FROM user_online;")
    # 將所有人下線
    _safe_exec(cur, "UPDATE users SET status='OFFLINE', last_login=NULL;")
    # 僅重置真的有 AUTOINCREMENT 的表
    _safe_exec(cur, "DELETE FROM sqlite_sequence WHERE name IN ('invites','ratings');")
    conn.commit()

    with ACTIVE_LOCK:
        ACTIVE_USERS.clear()

def reset_dev_runtime(db):
    conn = db.conn
    cur = conn.cursor()
    _safe_exec(cur, "PRAGMA foreign_keys = ON;")
    _safe_exec(cur, "UPDATE developers SET status='OFFLINE', last_login=NULL;")
    # 視需求清除 games 狀態（如果你希望 reset 也把遊戲清掉，可以打開下面兩行）
    # _safe_exec(cur, "DELETE FROM games;")
    # _safe_exec(cur, "DELETE FROM sqlite_sequence WHERE name IN ('games');")
    conn.commit()

class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.conn:
            # 確保外鍵有效
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self.conn.executescript(SCHEMA_SQL)

    def is_online(self, username: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "SELECT status FROM users WHERE username=?", (username,)
            )
            row = cur.fetchone()
            return bool(row and row["status"] == "ONLINE")

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
            with self.conn:
                self.conn.execute(
                    "UPDATE users SET status='ONLINE', last_login=datetime('now','localtime') WHERE username=?",
                    (username,),
                )
        return True, "login"

    def show_status(self, username: str):
        if not username:
            return False, "invalid args"
        with self.lock:
            cur = self.conn.execute(
                "SELECT username, status, last_login FROM users WHERE username=?",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return False, "no such user"
            info = {
                "username": row["username"],
                "status": row["status"],
                "last_login": row["last_login"],
            }
        return True, info

    def who(self, only_online: bool):
        sql = (
            "SELECT username, status FROM users "
            + ("WHERE status='ONLINE' " if only_online else "")
            + "ORDER BY username ASC"
        )
        with self.lock:
            rows = self.conn.execute(sql).fetchall()
        return [{"username": r["username"], "status": r["status"]} for r in rows]

    def create_room(self, room_id: str, owner: str, public: bool):
        try:
            with self.lock, self.conn:
                self.conn.execute(
                    "INSERT INTO rooms (id, owner, public, open) VALUES(?, ?, ?, 1)",
                    (room_id, owner, 1 if public else 0),
                )
                return True, "room created"
        except sqlite3.IntegrityError:
            return False, "room id exists"
        except Exception as e:
            return False, f"db error: {e}"

    def get_room(self, room_id: str):
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, owner, public, open FROM rooms WHERE id=?", (room_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "owner": row["owner"],
                "public": bool(row["public"]),
                "open": bool(row["open"]),
            }

    def list_rooms(self, only_public: bool = False):
        sql = "SELECT id, owner, public, open FROM rooms"
        if only_public:
            sql += " WHERE public=1"
        sql += " ORDER BY created_at DESC"
        with self.lock:
            rows = self.conn.execute(sql).fetchall()
        return [
            {
                "id": r["id"],
                "owner": r["owner"],
                "public": bool(r["public"]),
                "open": bool(r["open"]),
            }
            for r in rows
        ]
    
    def close_room(self, room_id: str):
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE rooms SET open=0 WHERE id=?", (room_id,)
            )
            return cur.rowcount > 0

    def delete_room(self, room_id: str):
        with self.lock, self.conn:
            cur = self.conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
            return cur.rowcount > 0

    # ========= DEV =========
    def dev_is_online(self, username: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "SELECT status FROM developers WHERE username=?", (username,)
            )
            row = cur.fetchone()
            return bool(row and row["status"] == "ONLINE")
        
    def dev_register(self, username: str, password: str):
        if not username or not password:
            return False, "invalid args"
        try:
            with self.lock, self.conn:
                self.conn.execute(
                    "INSERT INTO developers (username, password, status) VALUES(?, ?, 'OFFLINE')",
                    (username, password),
                )
                return True, "dev registered"
        except sqlite3.IntegrityError:
            return False, "dev exists"
        except Exception:
            return False, "db error"

    def dev_show_status(self, username: str):
        if not username:
            return False, "invalid args"
        with self.lock:
            cur = self.conn.execute(
                "SELECT username, status, last_login FROM developers WHERE username=?",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return False, "no such dev"
            info = {
                "username": row["username"],
                "status": row["status"],
                "last_login": row["last_login"],
            }
        return True, info

    def dev_login(self, username: str, password: str):
        if not username or not password:
            return False, "invalid args"
        with self.lock:
            cur = self.conn.execute(
                "SELECT id FROM developers WHERE username=? AND password=?",
                (username, password),
            )
            row = cur.fetchone()
            if not row:
                return False, "bad credential"
            with self.conn:
                self.conn.execute(
                    "UPDATE developers SET status='ONLINE', last_login=datetime('now','localtime') "
                    "WHERE username=?",
                    (username,),
                )
        return True, "dev login"

    def dev_logout(self, username: str):
        if not username:
            return False, "invalid args"
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE developers SET status='OFFLINE' WHERE username=?", (username,)
            )
            if cur.rowcount <= 0:
                return False, "no such dev"
        return True, "dev logout"

    def dev_list_games(self, owner: str):
        """列出某開發者擁有的所有遊戲"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, gamename, status, latest FROM games WHERE owner=? ORDER BY gamename ASC",
                (owner,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "gamename": r["gamename"],
                "status": r["status"],
                "latest": r["latest"],
            }
            for r in rows
        ]

    def dev_create_game(self, gamename: str, owner: str):
        if not gamename or not owner:
            return False, "invalid args"
        try:
            with self.lock, self.conn:
                self.conn.execute(
                    "INSERT INTO games (gamename, owner) VALUES(?, ?)",
                    (gamename, owner),
                )
            return True, "game created"
        except sqlite3.IntegrityError:
            return False, "game exists"
        except Exception:
            return False, "db error"

    def dev_is_owner(self, owner: str, gamename: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "SELECT 1 FROM games WHERE gamename=? AND owner=?",
                (gamename, owner),
            )
            return cur.fetchone() is not None

    def dev_update_game(self, owner: str, gamename: str, new_version: str):
        if not self.dev_is_owner(owner, gamename):
            return False, "not your game"
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE games SET latest=?, status='UPDATED' WHERE gamename=?",
                (new_version, gamename),
            )
            if cur.rowcount <= 0:
                return False, "no such game"
        return True, "updated"

    def dev_set_game_status(self, owner: str, gamename: str, new_status: str):
        # 用於上架 / 下架 / 停用等
        if not self.dev_is_owner(owner, gamename):
            return False, "not your game"
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE games SET status=? WHERE gamename=?",
                (new_status, gamename),
            )
            if cur.rowcount <= 0:
                return False, "no such game"
        return True, "status changed"

    # ========= Store / Player features =========
    def list_store_games(self):
        """列出所有可供玩家瀏覽的遊戲"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT gamename, owner, status, latest FROM games ORDER BY gamename ASC"
            ).fetchall()
        return [
            {
                "gamename": r["gamename"],
                "owner": r["owner"],
                "status": r["status"],
                "latest": r["latest"],
            }
            for r in rows
        ]

    def get_game_latest(self, gamename: str):
        with self.lock:
            cur = self.conn.execute(
                "SELECT gamename, status, latest FROM games WHERE gamename=?",
                (gamename,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "gamename": row["gamename"],
                "status": row["status"],
                "latest": row["latest"],
            }

    def upsert_download(self, username: str, gamename: str, version: str):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO downloads (username, gamename, version) VALUES(?,?,?) "
                "ON CONFLICT(username, gamename) DO UPDATE SET version=excluded.version, updated_at=datetime('now','localtime')",
                (username, gamename, version),
            )
        return True, "download recorded"

    def get_download(self, username: str, gamename: str):
        with self.lock:
            cur = self.conn.execute(
                "SELECT version, updated_at FROM downloads WHERE username=? AND gamename=?",
                (username, gamename),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"version": row["version"], "updated_at": row["updated_at"]}

    def list_downloads(self, username: str):
        with self.lock:
            rows = self.conn.execute(
                "SELECT gamename, version, updated_at FROM downloads WHERE username=? ORDER BY updated_at DESC",
                (username,),
            ).fetchall()
        return [
            {"gamename": r["gamename"], "version": r["version"], "updated_at": r["updated_at"]}
            for r in rows
        ]

    def add_rating(self, username: str, gamename: str, score: int, comment: str):
        if score < 1 or score > 5:
            return False, "score must be 1-5"
        # 確認下載紀錄以防未玩先評
        if not self.get_download(username, gamename):
            return False, "download required before rating"
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO ratings (gamename, username, score, comment) VALUES(?,?,?,?)",
                (gamename, username, score, comment),
            )
        return True, "rated"

    def list_ratings(self, gamename: str):
        with self.lock:
            rows = self.conn.execute(
                "SELECT username, score, comment, created_at FROM ratings WHERE gamename=? ORDER BY created_at DESC",
                (gamename,),
            ).fetchall()
        return [
            {
                "username": r["username"],
                "score": r["score"],
                "comment": r["comment"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

db = DB(DB_PATH)


def handle_user_client(conn: socket.socket, addr: tuple):
    authed_user = None

    try:
        while True:
            msg = recv_json(conn)
            if msg is None:
                break

            action = msg.get("action")
            if not action:
                send_json(conn, err("Missing action"))
                continue

            if action == "register":
                username = msg.get("username")
                password = msg.get("password")
                okb, message = db.register(username, password)
                send_json(conn, ok(message) if okb else err(message))

            elif action == "login":
                username = msg.get("username")
                password = msg.get("password")

                with ACTIVE_LOCK:
                    if username in ACTIVE_USERS:
                        send_json(conn, err("already online"))
                        continue

                if db.is_online(username):
                    send_json(conn, err("already online"))
                    continue

                okb, message = db.login(username, password)
                if okb:
                    authed_user = username
                    with ACTIVE_LOCK:
                        ACTIVE_USERS[username] = id(conn)
                send_json(conn, ok(message) if okb else err(message))

            elif action == "show_status":
                if authed_user is None:
                    send_json(conn, err("not logged in"))
                    continue
                target = msg.get("username")
                okb, message = db.show_status(target)
                send_json(conn, ok(message) if okb else err(message))

            elif action == "who_online":
                send_json(conn, ok(users=db.who(True)))
            # not used
            elif action == "all_who":
                send_json(conn, ok(users=db.who(False)))

            elif action == "create_room":
                room_id = msg.get("room_id")
                owner = msg.get("owner")
                public = msg.get("public", True)
                if not room_id or not owner:
                    send_json(conn, err("missing room_id or owner"))
                    continue
                okb, message = db.create_room(room_id, owner, public)
                send_json(conn, ok(message) if okb else err(message))
            # not used
            elif action == "get_room":
                room_id = msg.get("room_id")
                room = db.get_room(room_id)
                if room:
                    send_json(conn, ok(room=room))
                else:
                    send_json(conn, err("no such room"))
            
            elif action == "list_rooms":
                only_public = msg.get("only_public", False)
                rooms = db.list_rooms(only_public)
                send_json(conn, ok(rooms=rooms))

            elif action == "list_store_games":
                games = db.list_store_games()
                send_json(conn, ok(games=games))
            # need modify
            elif action == "download_game":
                user = authed_user or msg.get("username")
                if user is None:
                    send_json(conn, err("not logged in"))
                    continue
                gamename = msg.get("gamename")
                info = db.get_game_latest(gamename)
                if not info:
                    send_json(conn, err("no such game"))
                    continue
                # 紀錄下載至最新版本
                db.upsert_download(user, gamename, info["latest"])
                send_json(conn, ok("downloaded", game=info))

            elif action == "my_downloads":
                user = authed_user or msg.get("username")
                if user is None:
                    send_json(conn, err("not logged in"))
                    continue
                downloads = db.list_downloads(user)
                send_json(conn, ok(downloads=downloads))
            # not necessary
            elif action == "rate_game":
                user = authed_user or msg.get("username")
                if user is None:
                    send_json(conn, err("not logged in"))
                    continue
                gamename = msg.get("gamename")
                score = msg.get("score")
                comment = msg.get("comment", "")
                try:
                    score_int = int(score)
                except Exception:
                    send_json(conn, err("invalid score"))
                    continue
                okb, message = db.add_rating(user, gamename, score_int, comment)
                send_json(conn, ok(message) if okb else err(message))

            elif action == "list_ratings":
                gamename = msg.get("gamename")
                ratings = db.list_ratings(gamename)
                send_json(conn, ok(ratings=ratings))

            elif action == "close_room":
                room_id = msg.get("room_id")
                if db.close_room(room_id):
                    send_json(conn, ok("room closed"))
                else:
                    send_json(conn, err("no such room"))

            elif action == "delete_room":
                room_id = msg.get("room_id")
                if db.delete_room(room_id):
                    send_json(conn, ok("room deleted"))
                else:
                    send_json(conn, err("no such room"))

            elif action == "reset_runtime":
                reset_runtime(db)
                send_json(conn, ok("reset"))

            elif action == "quit":
                user = authed_user or msg.get("username")
                # 回覆先送出，避免前端等不到
                send_json(conn, ok("bye"))

                if user:
                    with db.lock, db.conn:
                        db.conn.execute(
                            "UPDATE users SET status='OFFLINE' WHERE username=?",
                            (user,)
                        )
                    with ACTIVE_LOCK:
                        ACTIVE_USERS.pop(user, None)
                    if user == authed_user:
                        authed_user = None
                break

            else:
                send_json(conn, err("unknown action"))

    except Exception as e:
        print(f"[DB] Error handling client {addr}: {e}")

def handle_dev_client(conn: socket.socket, addr: tuple):
    authed_dev = None

    try:
        while True:
            msg = recv_json(conn)
            if msg is None:
                break

            action = msg.get("action")
            if not action:
                send_json(conn, err("Missing action"))
                continue

            # ===== Developer auth =====
            if action == "dev_register":
                username = msg.get("username")
                password = msg.get("password")
                okb, message = db.dev_register(username, password)
                send_json(conn, ok(message) if okb else err(message))

            elif action == "dev_login":
                username = msg.get("username")
                password = msg.get("password")

                if db.dev_is_online(username):
                    send_json(conn, err("already online"))
                    continue

                okb, message = db.dev_login(username, password)
                if okb:
                    authed_dev = username
                send_json(conn, ok(message) if okb else err(message))

            elif action == "dev_logout":
                if authed_dev is None:
                    send_json(conn, err("not logged in"))
                    continue
                okb, message = db.dev_logout(authed_dev)
                authed_dev = None
                send_json(conn, ok(message) if okb else err(message))

            # ===== Developer status (optional) =====
            elif action == "dev_show_status":
                if authed_dev is None:
                    send_json(conn, err("not logged in"))
                    continue
                target = msg.get("username", authed_dev)
                okb, info = db.dev_show_status(target) if hasattr(db, "dev_show_status") else (False, "not implemented")
                send_json(conn, ok(info) if okb else err(info))

            # ===== Game management =====
            elif action == "dev_list_games":
                if authed_dev is None:
                    send_json(conn, err("not logged in"))
                    continue
                games = db.dev_list_games(authed_dev)
                send_json(conn, ok(games=games))

            elif action == "dev_create_game":
                if authed_dev is None:
                    send_json(conn, err("not logged in"))
                    continue
                gamename = msg.get("gamename")
                okb, message = db.dev_create_game(gamename, authed_dev)
                send_json(conn, ok(message) if okb else err(message))

            elif action == "dev_update_game":
                if authed_dev is None:
                    send_json(conn, err("not logged in"))
                    continue
                gamename = msg.get("gamename")
                version = msg.get("version")
                okb, message = db.dev_update_game(authed_dev, gamename, version)
                send_json(conn, ok(message) if okb else err(message))

            elif action == "dev_set_game_status":
                if authed_dev is None:
                    send_json(conn, err("not logged in"))
                    continue
                gamename = msg.get("gamename")
                new_status = msg.get("status")
                okb, message = db.dev_set_game_status(authed_dev, gamename, new_status)
                send_json(conn, ok(message) if okb else err(message))

            elif action == "dev_reset_runtime":
                # 如果你希望 dev 也能重置整個 runtime
                reset_dev_runtime(db)
                send_json(conn, ok("reset"))

            elif action == "quit":
                send_json(conn, ok("bye"))
                if authed_dev:
                    db.dev_logout(authed_dev)
                    authed_dev = None
                break

            else:
                send_json(conn, err("unknown action"))

    except Exception as e:
        print(f"[DB] Error handling dev client {addr}: {e}")

def handle_client(conn: socket.socket, addr: tuple):
    try:
        send_json(conn, {"status": "OK"})

        first = recv_json(conn)
        if first is None:
            return

        role = first.get("role")
        if role == "user":
            handle_user_client(conn, addr)
        elif role == "dev":
            handle_dev_client(conn, addr)
        else:
            send_json(conn, err("invalid role"))
            return
    except Exception as e:
        print(f"[DB] Error handling client {addr}: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        print(f"[DB] Client {addr} disconnected")

def run_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(64)
    print(f"[DB] Server listening on {HOST}:{PORT}")

    try:
        while True:
            conn, addr = srv.accept()
            print(f"[DB] Connected Lobby with {addr}")
            thread = threading.Thread(target=handle_client, args=(conn, addr))
            thread.daemon = True
            thread.start()
    except KeyboardInterrupt:
        print("\n[DB] Server shutting down...")
    finally:
        srv.close()


if __name__ == "__main__":
    run_server()
