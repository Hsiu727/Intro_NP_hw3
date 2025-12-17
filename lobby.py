import socket, threading
import contextlib, random, os
from typing import Dict
from utils import ok, err, send_json, recv_json, gen_room_id, with_req_id, send_file

# === setup ===
# HOST, PORT = "140.113.17.11", 18900
HOST, PORT = "localhost", 18900
DB_HOST, DB_PORT = "140.113.17.11", 19800
MAX_LEN = 65536

# ==== Lobby 狀態 ====
USERS: Dict[str, "ClientSession"] = {}     # username -> ClientSession
SESSIONS: Dict[int, "ClientSession"] = {}  # id(conn) -> ClientSession
ROOMS: Dict[str, "Room"] = {}              # room_id  -> Room
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


# ====== Minimal stub game server so lobby start/stop flows won't crash ======
class StubGameServer:
    def __init__(self, room_id: str, host: str, port: int, on_finish):
        self.room_id = room_id
        self.host = host
        self.port = port
        self.on_finish = on_finish
        self._running = threading.Event()
        self._running.set()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(8)
        self._serve_thread = threading.Thread(target=self._serve, daemon=True)
        # 自動結束計時器，避免長時間佔用 port
        self._auto_timer = threading.Timer(10.0, self.stop, kwargs={"reason": "auto_finish"})

    def start(self):
        self._serve_thread.start()
        self._auto_timer.start()

    def _serve(self):
        while self._running.is_set():
            try:
                self._srv.settimeout(1.0)
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed
            threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()

        with contextlib.suppress(Exception):
            self._srv.close()

    def _handle_client(self, conn, addr):
        try:
            msg = recv_json(conn)
            if msg and msg.get("type") == "force_stop":
                send_json(conn, {"status": "OK", "msg": "stopping"})
                self.stop(reason="forced_stop")
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def stop(self, reason: str = "stopped"):
        if not self._running.is_set():
            return
        self._running.clear()
        with contextlib.suppress(Exception):
            self._srv.close()
        with contextlib.suppress(Exception):
            self._auto_timer.cancel()
        if self.on_finish:
            try:
                self.on_finish(self.room_id, {"reason": reason})
            except Exception as e:
                print(f"[Lobby] on_finish callback error: {e}")


def start_game_server(room_id: str, host: str, port: int, on_finish):
    server = StubGameServer(room_id, host, port, on_finish)
    server.start()
    return server

# ==== 連 DB  ====
def db_call(payload: dict):
    try:
        with socket.create_connection((DB_HOST, DB_PORT), timeout=3) as s:
            send_json(s, {"role": "user"})
            send_json(s, payload)
            return recv_json(s)
    except Exception:
        return err("db unavailable")

def _room_status_payload(room):
    # room.players 為成員列表（字串 username）
    return {
        "event": "room_status",
        "room": {
            "id": room.id,
            "owner": room.owner,
            "public": bool(room.public),
            "open": bool(room.open),
            "members": list(room.players),
        },
    }

def _broadcast_room_status(room, exclude_user=None):
    payload = _room_status_payload(room)
    for u in list(room.players):
        if exclude_user and u == exclude_user:
            continue  # ← 不要再發給觸發者自己
        peer = USERS.get(u)
        if peer:
            with contextlib.suppress(Exception):
                send_json(peer.sock, payload)


class Room:
    def __init__(self, room_id: str, owner: str, public: bool = True):
        self.id = room_id
        self.owner = owner
        self.public = public
        self.players = [owner]
        self.open = True
        self.game = None

class ClientSession:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.authed = None
        self.room = None
        self.invitations = []

def _reset_room(room: Room, announce: bool = True):
    room.open = True
    room.game = None
    if announce:
        _broadcast_room_status(room)


def room_start(room: Room, user: str):
    # 啟動 game server，並且在結束後回房
    def _on_finish(rid, summary):
        # 1) DB：標記結束（可選）
        with contextlib.suppress(Exception):
            db_call({"action":"finish_game", "room_id": rid, "summary": summary})

        # 2) 重置房間狀態
        with LOCK:
            r = ROOMS.get(rid)
            if r:
                r.open = True
                r.game = None

        # 3) 推播「對局結束」與最新房況（可選）
        info = {"room": rid, **summary}  # 統一 finish 結構
        for p in list(room.players):
            peer = USERS.get(p)
            if peer:
                with contextlib.suppress(Exception):
                    send_json(peer.sock, {"event":"game_finished", "finish": info})
        _broadcast_room_status(room)  # ← 顯示 open=True、成員仍在

    port = allocate_port_in_range()
    try:
        start_game_server(room.id, host=GAME_BIND_HOST, port=port, on_finish=_on_finish)
    except OSError:
        port = allocate_port_in_range()
        start_game_server(room.id, host=GAME_BIND_HOST, port=port, on_finish=_on_finish)
    room.game = {"host": ADVERTISE_HOST, "port": port}  # 記錄給顯示用
    room.open = False
    # 更新 DB：關閉配對中之公開房（可選）
    try:
        db_call({"action": "close_room", "room_id": room.id})
    except Exception:
        pass
    # 推播開始
    # lobby.py → room_start(...)
    info = {"room": room.id, **room.game}
    for p in room.players:
        peer = USERS.get(p)
        # if p == user:
        #     continue
        if peer:
            send_json(peer.sock, {"event":"game_started", "game":info})


    print(f"[Lobby] Started game for room {room.id} at {GAME_BIND_HOST}:{port}, advertised as {ADVERTISE_HOST}:{port}")

def handle_register(conn, sess, msg):
    req_id = msg.get("req_id")
    username = msg.get("username")
    password = msg.get("password")
    db_resp = db_call({"action": "register", "username": username, "password": password})
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_login(conn, sess, msg):
    req_id = msg.get("req_id")
    username = msg.get("username")
    password = msg.get("password")

    with LOCK:
        if username in USERS:
            send_json(conn, err("already online", req_id=req_id))
            return True

    db_resp = db_call({"action": "login", "username": username, "password": password})
    if db_resp and db_resp.get("status") == "OK":
        with LOCK:
            sess.authed = username
            USERS[username] = sess
    send_json(conn, with_req_id(db_resp, req_id))

    db_resp = db_call({"action": "show_status", "username": username})
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_who_online(conn, sess, msg):
    req_id = msg.get("req_id")
    db_resp = db_call({"action": "who_online"})
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_create_room(conn, sess, msg):
    req_id = msg.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    public = msg.get("public", True)
    
    with LOCK:
        rid = gen_room_id()
        db_resp = db_call({
            "action": "create_room",
            "room_id": rid,
            "owner": sess.authed,
            "public": public
        })
        if db_resp.get("status") != "OK":
            send_json(conn, with_req_id(db_resp, req_id))
            return True

        room = Room(rid, sess.authed, public)
        ROOMS[rid] = room
        sess.room = rid
    
    send_json(conn, ok("room_created", req_id=req_id, room={
        "id": rid,
        "owner": room.owner,
        "public": room.public,
        "players": list(room.players),
        "open": room.open
    }))
    return True

def handle_list_rooms(conn, sess, msg):
    req_id = msg.get("req_id")
    db_resp = db_call({"action": "list_rooms", "only_public": True})
    if db_resp.get("status") == "OK":
        rooms = db_resp.get("rooms", [])
        # 合併 lobby 內存中的房間資訊
        with LOCK:
            for r in rooms:
                rid = r["id"]
                if rid in ROOMS:
                    r["players"] = list(ROOMS[rid].players)
                    r["open"] = ROOMS[rid].open
                else:
                    # 設為空房
                    r["players"] = []
        send_json(conn, ok(rooms=rooms, req_id=req_id))
    else:
        send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_list_store_games(conn, sess, msg):
    req_id = msg.get("req_id")
    db_resp = db_call({"action": "list_store_games"})
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_download_game(conn, sess, msg):
    req_id = msg.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    gamename = msg.get("gamename")
    db_resp = db_call({"action": "download_game", "gamename": gamename, "username": sess.authed})
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_my_downloads(conn, sess, msg):
    req_id = msg.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    db_resp = db_call({"action": "my_downloads", "username": sess.authed})
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_rate_game(conn, sess, msg):
    req_id = msg.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    gamename = msg.get("gamename")
    score = msg.get("score")
    comment = msg.get("comment", "")
    db_resp = db_call({
        "action": "rate_game",
        "gamename": gamename,
        "score": score,
        "comment": comment,
        "username": sess.authed
    })
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_list_ratings(conn, sess, msg):
    req_id = msg.get("req_id")
    gamename = msg.get("gamename")
    db_resp = db_call({"action": "list_ratings", "gamename": gamename})
    send_json(conn, with_req_id(db_resp, req_id))
    return True

def handle_join_room(conn, sess, msg):
    req_id = msg.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    rid = msg.get("room")
    
    with LOCK:
        room = ROOMS.get(rid)
        if not room:
            send_json(conn, err("no such room", req_id=req_id))
            return True
        if not room.public:
            send_json(conn, err("private room", req_id=req_id))
            return True
        if not room.open or room.game is not None:
            send_json(conn, err("room closed", req_id=req_id))
            return True
        if len(room.players) >= 2:
            send_json(conn, err("room full", req_id=req_id))
            return True
        if sess.authed in room.players:
            send_json(conn, ok("already in room", req_id=req_id))
            return True

        room.players.append(sess.authed)
        sess.room = rid
        with contextlib.suppress(Exception):
            send_json(conn, _room_status_payload(room))
            
        _broadcast_room_status(room, sess.authed)
                    
    send_json(conn, ok("joined", req_id=req_id, room=rid))
    return True

def handle_leave_room(conn, sess, msg):
    req_id = msg.get("req_id")
    rid = msg.get("room_id") or sess.room
    with LOCK:
        room = ROOMS.get(rid)
        if not room or sess.authed not in room.players:
            send_json(conn, err("not in room", req_id=req_id))
            return True

        room.players.remove(sess.authed)
        if room.owner == sess.authed:
            room.owner = room.players[0] if room.players else None
        if not room.players:
            ROOMS.pop(rid, None)
            with contextlib.suppress(Exception):
                db_call({"action":"delete_room", "room_id": rid})
            sess.room = None
            send_json(conn, ok("left", req_id=req_id))
            return True

        #    (a) 若沒有進行中的 game → 確保 open=True（允許他人加入）
        #    (b) 若正在對局但少於 2 人 → 判定對局結束、重置房間
        needs_reset = False
        if room.game is None:
            needs_reset = True
        elif len(room.players) < 2:
            needs_reset = True
            # 可選：通知仍在房內的那位玩家勝出（如果 game server 沒來得及 on_finish）
            survivor = room.players[0]
            peer = USERS.get(survivor)
            if peer:
                info = {"room": rid, "winner": survivor, "reason": "opponent_left_room"}
                with contextlib.suppress(Exception):
                    send_json(peer.sock, {"event":"game_finished", "finish": info})

        if needs_reset:
            _reset_room(room, announce=True)

    # 如果有進行中的遊戲，通知該 game server 強制結束
    if room.game:
        host = room.game.get("bind_host", room.game["host"])
        port = room.game["port"]

        try:
            # 用 socket 發一個 JSON 訊息告訴 game server 要 shutdown
            with socket.create_connection((host, port), timeout=1.0) as s:
                send_json(s, {"type": "force_stop"})
        except Exception as e:
            print(f"[Lobby] Failed to notify game server at {host}:{port}: {e}")

    send_json(conn, ok("left", req_id=req_id))
    return True

def handle_start_game(conn, sess, msg):
    req_id = msg.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    rid = msg.get("room_id") or msg.get("room")
    if not rid:
        send_json(conn, err("missing room_id", req_id=req_id))
        return True

    with LOCK:
        room = ROOMS.get(rid)
        if not room:
            send_json(conn, err("no such room", req_id=req_id))
            return True
        if room.owner != sess.authed:
            send_json(conn, err("only owner can start", req_id=req_id))
            return True
        if len(room.players) < 2:
            send_json(conn, err("need two players", req_id=req_id))
            return True
        if room.game is not None:
            send_json(conn, err("game already started", req_id=req_id))
            return True

    room_start(room, sess.authed)
    game_info = {"room": rid, **room.game}
    send_json(conn, ok("started", req_id=req_id, game=game_info))
    return True

def handle_invite(conn, sess, msg):
    req_id = msg.get("req_id")
    if not sess.authed or not sess.room:
        send_json(conn, err("not_in_room", req_id=req_id))
        return True
    target = msg.get("target")
    with LOCK:
        room = ROOMS.get(sess.room)
        if not room:
            send_json(conn, err("no such room", req_id=req_id))
            return True
        if len(room.players) >= 2:
            send_json(conn, err("room full", req_id=req_id))
            return True
        tgt = USERS.get(target)
        if not tgt:
            send_json(conn, err("target offline", req_id=req_id))
            return True
        tgt.invitations.append(
            {"type": "invite", "from": sess.authed, "room_id": room.id}
        )
    send_json(conn, ok("invite sent", req_id=req_id, target=target, room_id=room.id))
    return True

def handle_pull_notices(conn, sess, msg):
    req_id = msg.get("req_id")
    with LOCK:
        notices = list(sess.invitations)
        sess.invitations.clear()
    send_json(conn, ok(notices=notices, req_id=req_id))
    return True

def handle_accept_invite(conn, sess, msg):
    req_id = msg.get("req_id")
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    rid = msg.get("room_id")
    
    with LOCK:
        room = ROOMS.get(rid)
        if not room:
            send_json(conn, err("no such room", req_id=req_id))
            return True
        if len(room.players) >= 2:
            send_json(conn, err("room full", req_id=req_id))
            return True
        if sess.authed not in room.players:
            room.players.append(sess.authed)
            with contextlib.suppress(Exception):
                send_json(conn, _room_status_payload(room))
            _broadcast_room_status(room, sess.authed)
        sess.room = rid

    send_json(conn, ok("joined", req_id=req_id, room_id=rid))
    return True

def handle_quit(conn, sess, msg):
    req_id = msg.get("req_id")
    username = sess.authed
    rid = sess.room
    room = None
    # 先處理房內狀態（等同 leave_room）
    with LOCK:
        if rid:
            room = ROOMS.get(rid)
            if room and username in room.players:
                room.players.remove(username)
                # 移交或刪房
                if room.owner == username:
                    room.owner = room.players[0] if room.players else None
                if not room.players:
                    ROOMS.pop(rid, None)
                    with contextlib.suppress(Exception):
                        db_call({"action": "delete_room", "room_id": rid})
                else:
                    # 沒空房 → 重置房況並廣播
                    _reset_room(room, announce=True)

    # 若剛剛那間房正在開局，通知 game-server 強制結束（與 leave_room 同步）
    if room and room.game:
        host = room.game.get("bind_host", room.game["host"])
        port = room.game["port"]
        with contextlib.suppress(Exception):
            with socket.create_connection((host, port), timeout=1.0) as s:
                send_json(s, {"type": "force_stop"})

    # DB 登出、清線上清單
    db_call({"action": "quit", "username": username})
    with LOCK:
        USERS.pop(username, None)
        sess.authed = None
        sess.room = None
    send_json(conn, ok("bye", req_id=req_id))
    return False

def handle_download_game_file(conn, sess, msg):
    req_id = msg.get("req_id")
    # 為了避免混淆，將原本單純改 DB 的 download_game 保留，
    # 這個 action 專門負責傳檔案
    if not sess.authed:
        send_json(conn, err("not logged in", req_id=req_id))
        return True
    
    gamename = msg.get("gamename")
    
    # 1. 尋找檔案
    # 這裡簡化邏輯：假設每個遊戲資料夾下只有一個檔案，或者你需要 DB 紀錄檔名
    game_dir = os.path.join(UPLOAD_DIR, gamename)
    target_file = None
    
    if os.path.exists(game_dir):
        files = os.listdir(game_dir)
        if files:
            # 簡單起見，拿第一個檔案
            target_file = os.path.join(game_dir, files[0])
    
    if not target_file:
        send_json(conn, err("game_file_not_found", req_id=req_id))
        return True

    # 2. 告訴 Client 準備接收 (包含檔名)
    filename = os.path.basename(target_file)
    send_json(conn, ok("READY_TO_SEND", req_id=req_id, filename=filename))

    # 3. 發送檔案
    send_file(conn, target_file)
    
    # 4. (選用) 在此處呼叫 DB 記錄下載次數
    db_call({"action": "download_game", "gamename": gamename, "username": sess.authed})
    return True

COMMAND_HANDLERS = {
    "register": handle_register,
    "login": handle_login,
    "who_online": handle_who_online,
    "create_room": handle_create_room,
    "list_rooms": handle_list_rooms,
    "list_store_games": handle_list_store_games,
    "download_game": handle_download_game,
    "my_downloads": handle_my_downloads,
    "rate_game": handle_rate_game,
    "list_ratings": handle_list_ratings,
    "join_room": handle_join_room,
    "leave_room": handle_leave_room,
    "start_game": handle_start_game,
    "invite": handle_invite,
    "pull_notices": handle_pull_notices,
    "accept_invite": handle_accept_invite,
    "QUIT": handle_quit,
    "download_game_file": handle_download_game_file,
}

def handle_client(conn, addr):
    print(f"[Lobby] Client connected from {addr}")
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
            
            handler = COMMAND_HANDLERS.get(action)
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
            rid  = sess.room
        # 若還殘留，嘗試同樣的離房邏輯（簡化版）
        with LOCK:
            if rid:
                room = ROOMS.get(rid)
                if room and user in room.players:
                    room.players.remove(user)
                    if not room.players:
                        ROOMS.pop(rid, None)
                        with contextlib.suppress(Exception):
                            db_call({"action":"delete_room","room_id": rid})
            if user:
                USERS.pop(user, None)
            SESSIONS.pop(id(conn), None)
        with contextlib.suppress(Exception):
            conn.close()
        print(f"[Lobby] Client {addr} disconnected")

def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(128)
    print(f"[Lobby] listening on {HOST}:{PORT}")
    try:
        resp = db_call({"action": "reset_runtime"})
        if resp and resp.get("status") == "OK":
            print("[Lobby] reset_runtime OK:", resp.get("msg"))
        else:
            print("[Lobby] reset_runtime failed:", resp)
    except Exception as e:
        print("[Lobby] reset_runtime failed:", e)

    try:
        while True:
            conn, addr = srv.accept()
            thread = threading.Thread(target=handle_client, args=(conn, addr), daemon = True)
            thread.start()
    except KeyboardInterrupt:
        print("\n[Lobby] Server shutting down...")
    finally:
        srv.close()

if __name__ == "__main__":
    main()
