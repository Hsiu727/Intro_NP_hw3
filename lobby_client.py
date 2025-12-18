import socket
import threading
import sys
import os
import time
import queue
import subprocess
import shlex
import select

# 引入 utils 中的函式 (請確保 utils.py 已包含 recv_file)
from utils import send_json, recv_json, gen_req_id, recv_file

# 連線設定 (可透過環境變數覆寫)
HOST = os.getenv("LOBBY_HOST", "140.113.17.11")
PORT = int(os.getenv("LOBBY_PORT", "18900"))

class LobbyClient:
    def __init__(self):
        self.sock = None
        self.username = None
        self.running = True
        self.current_room_id = None
        self.pending_game_info = None
        
        # 用於存放等待中的 Request 回應 (req_id -> payload)
        self.response_queues = {}
        self.lock = threading.Lock()
        
        # 用於存放非同步通知 (如邀請、遊戲開始)，供 UI 顯示
        self.notification_queue = queue.Queue()
        self.game_process = None

    def connect(self):
        try:
            self.sock = socket.create_connection((HOST, PORT), timeout=None)
            print(f"[System] Connected to Lobby at {HOST}:{PORT}")
            
            # 啟動監聽執行緒
            self.listen_thread = threading.Thread(target=self._listener, daemon=True)
            self.listen_thread.start()
            return True
        except Exception as e:
            print(f"[Error] Connection failed: {e}")
            return False

    def close(self):
        self.running = False
        if self.sock:
            self.sock.close()

    def _listener(self):
        """
        背景執行緒：負責接收所有來自 Server 的訊息。
        包含：一般 JSON 回應、主動推播事件 (Events)、檔案傳輸串流。
        """
        while self.running and self.sock:
            try:
                # 1. 嘗試讀取標準 JSON 訊息
                msg = recv_json(self.sock)
                if msg is None:
                    print("\n[System] Disconnected from server.")
                    self.running = False
                    break

                # --- 特殊處理：檔案傳輸 ---
                # 檢查是否為檔案傳輸前的預告信號 (由 lobby.py 修改版發送)
                if msg.get("status") == "OK" and msg.get("msg") == "READY_TO_SEND":
                    filename = msg.get("filename", "game.py")
                    # 暫時通知 UI
                    print(f"\n[Download] Server is sending file: {filename}...")
                    
                    # 決定存檔路徑: downloads/{username}/{gamename}/filename
                    # 注意：這裡假設 Context 知道正在下載哪個遊戲，或者 Server 可以在 msg 帶入 gamename
                    # 為了簡化，我們先存到 downloads/temp/，後續邏輯再移動，或是依賴 msg 帶路徑
                    # 這裡示範簡單邏輯：從 response queue 找最近的一個 download request
                    # 但更穩健的是 Server 回傳時帶上 gamename，這裡假設 Server 有回傳
                    
                    # 這裡直接接收到當前目錄的暫存區，由主執行緒處理搬移，或是直接寫死路徑
                    # 為了配合作業目錄結構 downloads/{Player}/{Game}
                    # 我們假設在發送 download 请求時已經建立了資料夾，這裡我們需要知道存哪
                    # 這裡做一個簡單的全域變數或 Context 處理有點複雜，
                    # 簡單作法：Server 傳來的 msg 包含 gamename (建議修改 Server 端)
                    # 假設 Server 回傳: {"status":"OK", "msg":"READY_TO_SEND", "filename":"main.py", "req_id":...}
                    
                    # 透過 req_id 找到是誰發起的下載，雖然這裡是在 listener
                    req_id = msg.get("req_id")
                    
                    # 觸發接收二進制檔案
                    # 注意：這會阻塞 Listener，直到檔案收完，這正是我們要的
                    # 我們需要一個地方存。暫時存到 "download_cache"
                    os.makedirs("download_cache", exist_ok=True)
                    temp_path = os.path.join("download_cache", filename)
                    
                    success = recv_file(self.sock, temp_path)
                    
                    if success:
                        msg["download_path"] = temp_path # 將路徑注入回 msg
                        msg["status"] = "OK" # 確保狀態
                    else:
                        msg["status"] = "ERROR"
                        msg["msg"] = "File transfer failed"

                # 2. 分類訊息
                req_id = msg.get("req_id")
                event = msg.get("event")

                if req_id and req_id in self.response_queues:
                    # 這是對應某個 Request 的回應
                    self.response_queues[req_id].put(msg)
                
                elif event:
                    # 這是 Server 主動推播的事件 (Invite, GameStart, RoomStatus)
                    self._handle_event(msg)
                
                else:
                    # 其他未分類訊息，顯示出來 debug
                    # print(f"\n[Debug] Unknown msg: {msg}")
                    pass

            except Exception as e:
                # print(f"[Error] Listener error: {e}")
                pass

    def _handle_event(self, msg):
        event = msg.get("event")
        
        if event == "game_started":
            # [P3] 自動啟動遊戲
            game_info = msg.get("game", {})
            self.notification_queue.put(f"!!! 遊戲開始 !!! Room: {game_info.get('room')}")
            # 由主執行緒在房間等待流程中啟動，避免與輸入搶 stdin
            self.pending_game_info = game_info
            
        elif event == "room_status":
            # 更新房間顯示 (如果正在房間畫面的話)
            r = msg.get("room", {})
            if r.get("id") == self.current_room_id:
                # Sync the game name from the server
                self.current_gamename = r.get("gamename")
            
        elif event == "game_finished":
             self.notification_queue.put(f"--- 遊戲結束 --- Winner: {msg.get('finish',{}).get('winner')}")

        else:
            self.notification_queue.put(f"[Notification] {msg}")

    def _launch_game_client(self, game_info, block_on_fallback: bool = False):
        # 1. 取得遊戲名稱 (邏輯不變)
        gamename = game_info.get("gamename") or getattr(self, "current_gamename", None)
        if not gamename:
            print("[Error] 無法啟動遊戲：未知遊戲名稱")
            return

        game_path = os.path.join("downloads", self.username, gamename, "main.py")
        host = game_info.get("host", "localhost")
        port = str(game_info.get("port"))
        
        print(f"[System] 嘗試在新視窗啟動遊戲: {gamename} ...")
        
        try:
            # === Windows 系統 ===
            if sys.platform == "win32":
                self.game_process = subprocess.Popen(
                    [sys.executable, game_path, host, port],
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                )
            
            # === Linux 系統 (有桌面環境，如 Ubuntu/GNOME) ===
            elif sys.platform.startswith("linux"):
                # 嘗試使用常見的終端機模擬器
                try:
                    self.game_process = subprocess.Popen([
                        "gnome-terminal", "--", 
                        sys.executable, game_path, host, port
                    ])
                except FileNotFoundError:
                    # 如果沒有 gnome-terminal，嘗試 xterm
                    try:
                        self.game_process = subprocess.Popen([
                            "xterm", "-e", 
                            sys.executable, game_path, host, port
                        ])
                    except FileNotFoundError:
                        print("[System] 找不到可用的終端機視窗，將在當前視窗執行...")
                        # Fallback: 如果真的開不了新視窗，只好回到原本的同一視窗模式
                        if block_on_fallback:
                            subprocess.run([sys.executable, game_path, host, port])
                            return
                        self.game_process = subprocess.Popen([sys.executable, game_path, host, port])
                        print("\n>>> 請按 [Enter] 鍵將控制權交給遊戲 (重要！) <<<\n")

        except Exception as e:
            print(f"[Error] Failed to launch game: {e}")

    def call(self, action, **kwargs):
        """ 發送請求並等待回應 (同步模式) """
        req_id = gen_req_id()
        payload = {"action": action, "req_id": req_id}
        payload.update(kwargs)
        
        # 註冊 Queue
        q = queue.Queue()
        with self.lock:
            self.response_queues[req_id] = q
            
        send_json(self.sock, payload)
        
        try:
            # 等待回應 (Timeout 5秒)
            resp = q.get(timeout=10) 
            return resp
        except queue.Empty:
            return {"status": "ERROR", "msg": "Request timed out"}
        finally:
            with self.lock:
                self.response_queues.pop(req_id, None)

    # ================= UI / Menu Logic =================

    def print_notifications(self):
        """ 在重繪選單前，將累積的通知印出來 """
        while not self.notification_queue.empty():
            msg = self.notification_queue.get()
            print(f"\n>> {msg}")

    def clear_screen(self):
        # 簡單的分隔線代替清空，避免在 IDE 中出問題
        print("\n" + "="*40 + "\n")

    def start_ui(self):
        if not self.connect():
            return

        while self.running:
            if not self.username:
                self.menu_auth()
            else:
                self.menu_lobby()

    def menu_auth(self):
        self.clear_screen()
        print("=== 歡迎來到遊戲商城大廳 (Player) ===")
        print("1. 登入")
        print("2. 註冊")
        print("3. 離開")
        
        choice = input("請選擇 (1-3): ").strip()
        
        if choice == "1":
            user = input("帳號: ").strip()
            pwd = input("密碼: ").strip()
            resp = self.call("login", username=user, password=pwd)
            if resp.get("status") == "OK":
                self.username = user
                print("登入成功！")
            else:
                print(f"登入失敗: {resp.get('msg')}")
                input("按 Enter 繼續...")
                
        elif choice == "2":
            user = input("帳號: ").strip()
            pwd = input("密碼: ").strip()
            resp = self.call("register", username=user, password=pwd)
            print(f"註冊結果: {resp.get('msg')}")
            input("按 Enter 繼續...")
            
        elif choice == "3":
            self.close()
            sys.exit(0)

    def menu_lobby(self):
        while self.username and self.running:
            self.clear_screen()
            self.print_notifications()
            print(f"=== 大廳主選單 (User: {self.username}) ===")
            print("1. [P1] 瀏覽商城 / 下載遊戲")
            print("2. 我的下載 (已安裝遊戲)")
            print("3. 房間列表 / 加入房間")
            print("4. [P3] 建立房間")
            print("5. 登出")
            
            choice = input("請選擇功能: ").strip()
            
            if choice == "1":
                self.ui_store()
            elif choice == "2":
                self.ui_my_downloads()
            elif choice == "3":
                self.ui_room_list()
            elif choice == "4":
                self.ui_create_room()
            elif choice == "5":
                self.call("quit")
                self.username = None
                return
            
            # 處理可能進入房間後的狀態 (若 current_room_id 被設定)
            if self.current_room_id:
                self.menu_room_wait()

    def ui_store(self):
        """ [P1] 瀏覽商城與 [P2] 下載 """
        resp = self.call("list_store_games")
        games = resp.get("games", [])
        
        if not games:
            print("商城目前沒有遊戲。")
            input("Wait...")
            return

        while True:
            self.clear_screen()
            print("=== 遊戲商城 ===")
            print(f"{'No.':<4} {'Game Name':<15} {'Version':<10} {'Status'}")
            for i, g in enumerate(games):
                print(f"{i+1:<4} {g['gamename']:<15} {g['latest']:<10} {g['status']}")
            print("0. 返回")
            
            sel = input("輸入編號查看詳情/下載 (0 返回): ").strip()
            if sel == "0": break
            
            try:
                idx = int(sel) - 1
                if 0 <= idx < len(games):
                    self.ui_game_detail(games[idx])
                else:
                    print("無效編號")
            except ValueError:
                pass

    def ui_game_detail(self, game_info):
        """ [P1] 詳細資訊 & [P2] 下載邏輯 """
        gn = game_info['gamename']
        print(f"\n--- {gn} ---")
        print(f"擁有者: {game_info['owner']}")
        print(f"最新版本: {game_info['latest']}")
        # 這裡還可以呼叫 list_ratings 顯示評價 [P1]
        
        print("\n1. [P2] 下載/更新此遊戲")
        print("2. [P4] 查看評價")
        print("3. 返回")
        
        op = input("選擇: ").strip()
        if op == "1":
            self.perform_download(gn)
        elif op == "2":
            self.ui_ratings(gn)

    def perform_download(self, gamename):
        """ [P2] 執行下載流程 (整合檔案傳輸) """
        print(f"正在請求下載 {gamename}...")
        
        # 1. 發送特殊的 download_game_file 請求 (根據上一輪對話的 Server 修改)
        resp = self.call("download_game_file", gamename=gamename)
        
        if resp.get("status") == "OK" and "download_path" in resp:
            temp_path = resp["download_path"]
            filename = os.path.basename(temp_path)
            
            # 2. 移動檔案到正確位置 downloads/{Player}/{Game}/
            target_dir = os.path.join("downloads", self.username, gamename)
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, filename)
            
            # 簡單的搬移 (如果跨 filesystem 需用 shutil)
            try:
                if os.path.exists(target_path):
                    os.remove(target_path) # 覆蓋舊版
                os.replace(temp_path, target_path)
                
                # 賦予執行權限 (Linux)
                import stat
                st = os.stat(target_path)
                os.chmod(target_path, st.st_mode | stat.S_IEXEC)
                
                print(f"[Success] 遊戲已下載至: {target_path}")
                
                # 更新 DB 紀錄 (因為 lobby.py 修改版可能在傳完檔後沒寫入 downloads 表)
                # 這裡補發一個純紀錄用的 request 比較保險，或是依賴 server 邏輯
                self.call("download_game", gamename=gamename) 
                
            except Exception as e:
                print(f"[Error] 檔案搬移失敗: {e}")
        else:
            print(f"[Error] 下載失敗: {resp.get('msg')}")
        
        input("按 Enter 繼續...")

    def ui_my_downloads(self):
        resp = self.call("my_downloads")
        dls = resp.get("downloads", [])
        print("\n=== 我的已安裝遊戲 ===")
        for d in dls:
            print(f"- {d['gamename']} (Ver: {d['version']})")
        input("按 Enter 繼續...")

    def ui_ratings(self, gamename):
        resp = self.call("list_ratings", gamename=gamename)
        ratings = resp.get("ratings", [])
        print(f"\n=== {gamename} 的評價 ===")
        for r in ratings:
            print(f"[{r['score']}分] {r['username']}: {r['comment']}")
        
        # [P4] 撰寫評價
        do_rate = input("\n要撰寫評價嗎? (y/n): ").lower()
        if do_rate == 'y':
            score = input("分數 (1-5): ")
            comment = input("留言: ")
            try:
                res = self.call("rate_game", gamename=gamename, score=score, comment=comment)
                print("結果:", res.get("msg"))
            except:
                pass
            input("Wait...")

    def ui_room_list(self):
        # 取得公開房間列表
        resp = self.call("list_rooms")
        rooms = resp.get("rooms", [])
        
        while True:
            self.clear_screen()
            print("=== 房間列表 ===")
            for i, r in enumerate(rooms):
                status = "OPEN" if r['open'] else "PLAYING"
                print(f"{i+1}. Room {r['id']} (Owner: {r['owner']}) [{status}] - {len(r['players'])}/2")
            
            print("0. 返回")
            print("R. 重新整理")
            
            sel = input("輸入編號加入 (0 返回): ").strip().upper()
            if sel == "0": return
            if sel == "R": 
                resp = self.call("list_rooms")
                rooms = resp.get("rooms", [])
                continue
                
            try:
                idx = int(sel) - 1
                if 0 <= idx < len(rooms):
                    rid = rooms[idx]['id']
                    res = self.call("join_room", room=rid)
                    if res.get("status") == "OK":
                        self.current_room_id = rid
                        # 進入房間等待畫面 (這裡假設不知道遊戲名稱，需 Server 補強)
                        # 暫時設為 None，等 Room Status 更新
                        self.current_gamename = None 
                        return
                    else:
                        print("加入失敗:", res.get("msg"))
                        input("...")
            except ValueError:
                pass

    def ui_create_room(self):
        """ [P3] 建立房間流程 """
        # 先選遊戲
        print("請先選擇要遊玩的遊戲 (必須已下載):")
        resp = self.call("my_downloads")
        dls = resp.get("downloads", [])
        
        if not dls:
            print("你還沒下載任何遊戲，無法開房。")
            input("...")
            return

        for i, d in enumerate(dls):
            print(f"{i+1}. {d['gamename']}")
        
        sel = input("選擇遊戲 (0 取消): ")
        if sel == "0": return
        
        try:
            idx = int(sel) - 1
            if 0 <= idx < len(dls):
                target_game = dls[idx]['gamename']
                self.current_gamename = target_game # 記住現在要玩啥
                
                # 發送建房請求 (目前 Server 的 create_room 只有 public 參數，沒有綁定 gamename)
                # 注意：根據 Server 邏輯，房間本身沒綁定遊戲，是 start_game 時才決定?
                # 還是我們應該把 gamename 存在 Client 的 session 裡?
                # 為了符合 [P3] 邏輯，我們假設開房是為了玩特定遊戲。
                
                res = self.call("create_room", public=True, gamename=target_game)
                if res.get("status") == "OK":
                    room_info = res.get("room")
                    self.current_room_id = room_info['id']
                    print(f"房間 {self.current_room_id} 建立成功！")
                    return
                else:
                    print("建立失敗:", res.get("msg"))
                    input("...")
        except ValueError:
            pass

    def menu_room_wait(self):
        print(f"\n=== 房間: {self.current_room_id} ===")
        print("等待其他玩家中...")
        print("如果是房主，當人數足夠時可輸入 'start' 開始遊戲")
        print("輸入 'leave' 離開房間")
        
        while self.current_room_id:
            # 若收到 game_started，這裡負責啟動遊戲，避免 listener 搶 stdin
            if self.pending_game_info:
                game_info = self.pending_game_info
                self.pending_game_info = None
                self._launch_game_client(game_info, block_on_fallback=True)
                self.clear_screen()
                print(f"=== 房間: {self.current_room_id} (遊戲結束) ===")
                print("輸入 'start' 再次開始，或 'leave' 離開")
                continue

            # [Check 1] 迴圈開始前，先檢查是否有遊戲正在跑 (針對 P1 剛按完 start 的情況)
            if self.game_process:
                self.game_process.wait() # 這裡會暫停 Lobby，直到遊戲結束
                self.game_process = None
                # 遊戲結束後，重繪介面
                self.clear_screen()
                print(f"=== 房間: {self.current_room_id} (遊戲結束) ===")
                print("輸入 'start' 再次開始，或 'leave' 離開")
                continue # 跳過這一次 input，重新開始迴圈

            # 正常等待輸入 (非阻塞輪詢，避免卡住錯過 game_started)
            if sys.platform.startswith("linux"):
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not ready:
                    continue
                cmd = sys.stdin.readline().strip().lower()
            else:
                cmd = input("(Room) > ").strip().lower()

            # [Check 2] 使用者按下 Enter 後 (針對 P2 被動接收通知的情況)
            # 如果此時遊戲剛好啟動了，game_process 會有值
            if self.game_process:
                self.game_process.wait() # 讓出控制權
                self.game_process = None
                self.clear_screen()
                print(f"=== 房間: {self.current_room_id} (遊戲結束) ===")
                continue

            # ... (原本的指令處理) ...
            if cmd == "leave":
                self.call("leave_room")
                self.current_room_id = None
                self.current_gamename = None
                break
            
            elif cmd == "start":
                # ... (原本的 start 邏輯) ...
                res = self.call("start_game", room_id=self.current_room_id)
                if res.get("status") != "OK":
                    print("開始失敗:", res.get("msg"))
                # 如果成功，Server 會送 game_started 事件 -> _launch_game_client 設定 self.game_process
                # 迴圈下一輪 [Check 1] 就會抓到並進入等待狀態

if __name__ == "__main__":
    client = LobbyClient()
    try:
        client.start_ui()
    except KeyboardInterrupt:
        client.close()
        print("\nBye.")
