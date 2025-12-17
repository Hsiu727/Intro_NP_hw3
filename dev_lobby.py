import socket, threading
import contextlib, random, os
from typing import Dict
from utils import ok, err, send_json, recv_json, gen_room_id, with_req_id, recv_file

# === setup ===
# HOST, PORT = "140.113.17.11", 18950
HOST, PORT = "localhost", 18950
DB_HOST, DB_PORT = "140.113.17.11", 19800
MAX_LEN = 65536

# ==== Lobby 狀態 ====
DEVS: Dict[str, "ClientSession"] = {}     # username -> ClientSession
SESSIONS: Dict[int, "ClientSession"] = {}  # id(conn) -> ClientSession
LOCK = threading.Lock()

GAME_BIND_HOST = os.getenv("GAME_BIND_HOST", "0.0.0.0")  # 遊戲伺服器綁定 IP
ADVERTISE_HOST = os.getenv("ADVERTISE_HOST", "140.113.17.11")           # 廣播給 Client 的 IP（可手動指定）
PORT_MIN, PORT_MAX = 10000, 60000
UPLOAD_DIR = "server_games"

def allocate_port_in_range() -> int:
    candidates = list(range(PORT_MIN, PORT_MAX))
    random.shuffle(candidates)
    for p in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((GAME_BIND_HOST, p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"No free port in range {PORT_MIN}-{PORT_MAX}")

# ==== 連 DB  ====
def db_call(payload: dict):
    try:
        with socket.create_connection((DB_HOST, DB_PORT), timeout=3) as s:
            send_json(s, {"role": "dev"})
            send_json(s, payload)
            return recv_json(s)
    except Exception:
        return err("db unavailable")

class ClientSession:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.authed = None
  
def handle_client(conn, addr):
    print(f"[DEV_Lobby] Client connected from {addr}")
    sess = ClientSession(conn)
    with LOCK:
        SESSIONS[id(conn)] = sess

    try:
        while True:
            req = recv_json(conn)
            if req is None:
                break

            action = req.get("action")
            req_id = req.get("req_id")
            if not action:
                send_json(conn, err("missing action", req_id=req_id))
                continue
            # ---------- 使用者註冊/登入/登出（走 DB） ----------
            if action == "register":
                username = req.get("username")
                password = req.get("password")
                db_resp = db_call({"action": "dev_register", "username": username, "password": password})
                send_json(conn, with_req_id(db_resp, req_id))

            elif action == "login":
                username = req.get("username")
                password = req.get("password")

                with LOCK:
                    if username in DEVS:
                        send_json(conn, err("already online", req_id=req_id))
                        continue

                db_resp = db_call({"action": "dev_login", "username": username, "password": password})
                if db_resp and db_resp.get("status") == "OK":
                    with LOCK:
                        sess.authed = username
                        DEVS[username] = sess
                send_json(conn, with_req_id(db_resp, req_id))

                # db_resp = db_call({"action": "show_status", "username": username})
                # send_json(conn, with_req_id(db_resp, req_id))

            elif action == "list_games":
                if not sess.authed:
                    send_json(conn, err("not logged in", req_id=req_id)); continue
                db_resp = db_call({
                    "action": "dev_list_games",
                    "owner": sess.authed,   # 其實 DB 裡 dev_list_games(authed_dev) 已經無視這個參數
                })
                send_json(conn, with_req_id(db_resp, req_id))
            
            elif action == "create_game":
                if not sess.authed:
                    send_json(conn, err("not logged in", req_id=req_id)); continue
                gamename = req.get("gamename")
                db_resp = db_call({"action": "dev_create_game","gamename": gamename})
                send_json(conn, with_req_id(db_resp, req_id))

            elif action == "update_game":
                if not sess.authed:
                    send_json(conn, err("not logged in", req_id=req_id)); continue
                gamename = req.get("gamename")
                version  = req.get("version")
                db_resp = db_call({
                    "action": "dev_update_game",
                    "gamename": gamename,
                    "version": version,
                })
                send_json(conn, with_req_id(db_resp, req_id))
            
            elif action == "set_game_status":
                if not sess.authed:
                    send_json(conn, err("not logged in", req_id=req_id)); continue
                gamename = req.get("gamename")
                status   = req.get("status")   # e.g. "UNLOADED", "PUBLISHED", ...
                db_resp = db_call({
                    "action": "dev_set_game_status",
                    "gamename": gamename,
                    "status": status,
                })
                send_json(conn, with_req_id(db_resp, req_id))
            
            elif action == "upload_game_file":
                if not sess.authed:
                    send_json(conn, err("not logged in", req_id=req_id)); continue
                
                gamename = req.get("gamename")
                filename = req.get("filename")
                
                # 這裡應該要先檢查 DB 是否為該遊戲擁有者 (省略 DB 檢查程式碼以簡化)
                # is_owner = db_call(...) 
                
                # 1. 建立存放路徑: server_games/<gamename>/
                save_dir = os.path.join(UPLOAD_DIR, gamename)
                os.makedirs(save_dir, exist_ok=True)
                save_path = os.path.join(save_dir, filename)

                send_json(conn, ok("READY_TO_RECV", req_id=req_id))

                print(f"Receiving file for {gamename}...")
                success = recv_file(conn, save_path)
                
                if success:
                    # 4. 更新 DB (選用)：記錄檔案路徑或更新版本時間
                    # db_call({"action": "update_path", ...}) 
                    send_json(conn, ok("upload_success", req_id=req_id))
                    print(f"File saved to {save_path}")
                else:
                    send_json(conn, err("upload_failed", req_id=req_id))
            
            else:
                send_json(conn, err("unknown_cmd", req_id=req_id))
    
    except Exception as e:
        print(f"[Lobby] Error handling client {addr}: {e}")
    finally:
        with LOCK:
            user = sess.authed
        with LOCK:
            if user:
                DEVS.pop(user, None)
            SESSIONS.pop(id(conn), None)
        with contextlib.suppress(Exception):
            conn.close()
        print(f"[DEV Lobby] Client {addr} disconnected")

def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(128)
    print(f"[DEV Lobby] listening on {HOST}:{PORT}")
    try:
        resp = db_call({"action": "reset_dev_runtime"})
        if resp and resp.get("status") == "OK":
            print("[DEV Lobby] reset_runtime OK:", resp.get("msg"))
        else:
            print("[DEV Lobby] reset_runtime failed:", resp)
    except Exception as e:
        print("[DEV Lobby] reset_runtime failed:", e)

    try:
        while True:
            conn, addr = srv.accept()
            thread = threading.Thread(target=handle_client, args=(conn, addr), daemon = True)
            thread.start()
    except KeyboardInterrupt:
        print("\n[DEV Lobby] Server shutting down...")
    finally:
        srv.close()

if __name__ == "__main__":
    main()