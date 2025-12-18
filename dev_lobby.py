import socket, threading
import contextlib, random, os
from typing import Dict
from utils import ok, err, send_json, recv_json, gen_room_id, with_req_id, recv_file

# === setup ===
HOST, PORT = "140.113.17.11", 18950
# HOST, PORT = "localhost", 18950
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
            req = payload.copy()
            req["role"] = "dev"
            send_json(s, req)
            resp = recv_json(s)
            return resp if isinstance(resp, dict) else err("db protocol error")
    except Exception:
        return err("db unavailable")

class ClientSession:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.authed = None
  
def handle_register(conn, sess, req):
    req_id = req.get("req_id")
    username = req.get("username")
    password = req.get("password")
    db_resp = db_call({"action": "dev_register", "username": username, "password": password})
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_login(conn, sess, req):
    req_id = req.get("req_id")
    username = req.get("username")
    password = req.get("password")

    with LOCK:
        if username in DEVS:
            send_json(conn, err("already online", req_id=req_id))
            return True

    db_resp = db_call({"action": "dev_login", "username": username, "password": password})
    if db_resp and db_resp.get("status") == "OK":
        with LOCK:
            sess.authed = username
            DEVS[username] = sess
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_list_games(conn, sess, req):
    req_id = req.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    db_resp = db_call({
        "action": "dev_list_games",
        "owner": sess.authed,
    })
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_create_game(conn, sess, req):
    req_id = req.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    gamename = req.get("gamename")
    db_resp = db_call({"action": "dev_create_game", "gamename": gamename, "owner": sess.authed})
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_update_game(conn, sess, req):
    req_id = req.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    gamename = req.get("gamename")
    version  = req.get("version")
    db_resp = db_call({
        "action": "dev_update_game",
        "owner": sess.authed,
        "gamename": gamename,
        "version": version,
    })
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_set_game_status(conn, sess, req):
    req_id = req.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    gamename = req.get("gamename")
    status   = req.get("status")
    db_resp = db_call({
        "action": "dev_set_game_status",
        "owner": sess.authed,
        "gamename": gamename,
        "status": status,
    })
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_upload_game_file(conn, sess, req):
    req_id = req.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    
    filename = req.get("filename")
    
    # 1. 安全過濾 gamename
    raw_gamename = req.get("gamename", "default_game")
    # 只保留安全字元，防止路徑攻擊
    safe_gamename = "".join([c for c in raw_gamename if c.isalnum() or c in ('-', '_')]).strip()
    
    if not safe_gamename:
        safe_gamename = "unknown_game"
    
    if not filename.endswith(".py"):
        send_json(conn, err("Only .py files are allowed", req_id=req_id))
        return True
    
    save_dir = os.path.join(UPLOAD_DIR, safe_gamename)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, "main.py")

    send_json(conn, ok("READY_TO_RECV", req_id=req_id))

    print(f"Receiving file for {safe_gamename}...")
    success = recv_file(conn, save_path)
    
    if success:
        # 將上傳後的檔案路徑回寫到 DB，供玩家下載/啟動時查詢
        with contextlib.suppress(Exception):
            db_call({
                "action": "dev_update_game_path",
                "owner": sess.authed,
                "gamename": safe_gamename,
                "file_path": save_path,
            })
        send_json(conn, ok("upload_success", req_id=req_id))
        print(f"File saved to {save_path}")
    else:
        send_json(conn, err("upload_failed", req_id=req_id))
    return True

DEV_COMMAND_HANDLERS = {
    "register": handle_register,
    "login": handle_login,
    "list_games": handle_list_games,
    "create_game": handle_create_game,
    "update_game": handle_update_game,
    "set_game_status": handle_set_game_status,
    "upload_game_file": handle_upload_game_file,
}

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
            
            handler = DEV_COMMAND_HANDLERS.get(action)
            if handler:
                if not handler(conn, sess, req):
                    break
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
