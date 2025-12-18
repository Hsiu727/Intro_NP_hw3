import sys
import socket
import threading
import time
import os
import random

# 確保能 import utils (假設 main.py 與 utils.py 在同一層或正確路徑)
# 在 lobby_client 下載結構中，通常 utils.py 不會被下載
# 為了簡便，我們這裡直接複製 send_json/recv_json 的精簡版，或是假設環境有 utils
try:
    from utils import send_json, recv_json
except ImportError:
    # Fallback 如果找不到 utils，這裡提供最小依賴
    import json, struct
    def send_json(sock, obj):
        body = json.dumps(obj).encode("utf-8")
        sock.sendall(struct.pack("!I", len(body)) + body)
        return True
    def recv_json(sock):
        try:
            hdr = sock.recv(4)
            if not hdr: return None
            (l,) = struct.unpack("!I", hdr)
            return json.loads(sock.recv(l).decode("utf-8"))
        except: return None

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

class NumberGuessGame:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, int(port)))
        self.min_val = 0
        self.max_val = 100
        self.my_turn = False
        self.running = True
        self.role = "UNKNOWN" # HOST or GUEST

    def start(self):
        print(f"已連線到遊戲伺服器 {self.sock.getpeername()}")
        
        # 簡單的握手協定：先送出 Hello，決定誰是先手
        # 由於 Server 只是廣播，我們用一個簡單的方式：
        # 先發送 "HELLO" 的人當 HOST (設定密碼者)，後到的當 GUEST (猜題者)
        # 但因為 Server 不會告訴我們順序，我們用「等待對手」的方式
        
        # 發送加入訊息
        send_json(self.sock, {"type": "join", "time": time.time()})
        
        # 啟動接收執行緒
        threading.Thread(target=self._listener, daemon=True).start()
        
        # 主迴圈
        try:
            self._game_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.sock.close()

    def _listener(self):
        while self.running:
            msg = recv_json(self.sock)
            if not msg:
                print("\n[!] 與伺服器斷線或對手離開。")
                self.running = False
                os._exit(0) # 強制結束
            
            mtype = msg.get("type")
            
            if mtype == "join":
                # 對手加入了！如果你還沒確認身分，那你就是 HOST
                if self.role == "UNKNOWN":
                    self.role = "HOST"
                    print("\n>> 對手已加入！你是 [莊家]，請設定終極密碼！")
                    self.my_turn = True 

            elif mtype == "set_answer":
                # 收到對手設定的答案 (其實這裡我們不該傳答案過來防作弊，但為了 Demo 方便，我們用廣播同步狀態)
                # 為了公平，我們只接收 "range" 或 "start_guess"
                print("\n>> 莊家已設定密碼，遊戲開始！")
                self.role = "GUEST"
                self.my_turn = True # 莊家設完，換閒家猜
                
            elif mtype == "guess":
                val = msg.get("num")
                print(f"\n>> 對手猜了: {val}")
                self._update_range(val)
                self.my_turn = True # 換我猜
            
            elif mtype == "game_over":
                winner = msg.get("winner") # REMOTE or LOCAL
                print(f"\n>> 遊戲結束！獲勝者: {'你' if winner == 'REMOTE' else '對手'}")
                self.running = False
                print("請按 Enter 離開...")

    def _update_range(self, val):
        # 假設密碼是透過口頭或其他方式確認，或是這是一個合作遊戲
        # 為了簡化「終極密碼」邏輯：
        # 其實正確做法是 Server 判斷，但我們是廣播 Server
        # 所以這裡讓收到猜測的人輸入「太大/太小/猜中」來回傳？
        # 不，這樣太慢。我們改為：
        # 莊家輸入密碼後，存在本地。對手猜，莊家程式自動判斷並回傳結果。
        pass # 下面 _game_loop 處理詳細互動

    def _game_loop(self):
        # 1. 決定角色階段
        print("等待對手連線中...")
        while self.role == "UNKNOWN":
            time.sleep(0.5)
            # 如果過了很久都沒人加入，可能你是後進來的，試著發個訊號
            if self.role == "UNKNOWN":
                # 這裡是一個簡單的競態解決，實際作業可由 Lobby 指定 P1/P2
                pass

        # 2. 莊家設定密碼階段
        target = None
        if self.role == "HOST":
            while True:
                try:
                    ans = int(input(f"請輸入終極密碼 ({self.min_val}-{self.max_val}): "))
                    if self.min_val < ans < self.max_val:
                        target = ans
                        break
                    print("數字超出範圍！")
                except ValueError: pass
            
            # 通知對手我設好了
            send_json(self.sock, {"type": "set_answer"})
            print("等待對手猜測...")
            self.my_turn = False # 換對手

        # 3. 猜測迴圈
        while self.running:
            if self.my_turn:
                print(f"\n[你的回合] 目前範圍: {self.min_val} < ? < {self.max_val}")
                try:
                    guess = int(input("請輸入猜測數字: "))
                except ValueError:
                    continue

                if not (self.min_val < guess < self.max_val):
                    print("無效範圍，請重輸。")
                    continue
                
                # 送出猜測
                send_json(self.sock, {"type": "guess", "num": guess})
                
                # 如果我是莊家(HOST)，我自己不用猜，我是在這迴圈等對手猜，
                # 但上面的邏輯是「輪流猜」的寫法。
                # 終極密碼通常是：多人輪流猜，範圍縮小。
                # 我們修正玩法：兩人都猜 (公平版)，或者 莊家看戲版。
                
                # 這裡採用：兩人輪流猜同一個密碼 (但密碼在兩人的心中？不，我們讓雙方同步範圍)
                # 為了簡化：假設密碼是 **隨機產生** 的，雙方都不知道，看誰猜中「踩到地雷」或是「猜中獲勝」。
                # 我們玩：猜中者獲勝。
                
                # 本地更新範圍 (這是最簡單的同步)
                # 但為了要有正確答案，我們需要一個 Truth。
                # 我們改回：HOST 設定密碼，GUEST 猜。
                # 但這樣 HOST 沒事做。
                
                # === 最終玩法：HOST設定密碼，GUEST猜，HOST程式自動回報結果 ===
                pass 
            else:
                # 等待對方動作
                time.sleep(0.1)

            # 這裡為了展示「互動」，我們改寫為：
            # 雙方都不知道密碼，密碼由第一個人隨機亂數決定並廣播 Hash (防作弊)，
            # 然後兩人輪流猜。
            pass

# main.py 修正版 (只替換 UltimatePasswordShared 類別與 main 區塊)

class UltimatePasswordShared(NumberGuessGame):
    def start(self):
        print(f"連線至 {self.sock.getpeername()}...")
        # 1. 產生一個隨機 ID 用來比大小
        self.my_id = random.randint(1, 1000000)
        self.answer = None
        
        # 2. 發送加入訊息 (廣播)
        send_json(self.sock, {"type": "join", "id": self.my_id})
        
        threading.Thread(target=self._listener, daemon=True).start()
        
        print(f"等待對手... (My ID: {self.my_id})")
        while self.role == "UNKNOWN":
            time.sleep(0.1)
            
        self._game_loop()

    def _listener(self):
        while self.running:
            msg = recv_json(self.sock)
            if not msg: 
                self.running = False
                print("\n[!] 連線中斷"); os._exit(0)
            
            mt = msg.get("type")
            
            # === 收到對方的 Join 或 Presence (回應) ===
            if mt == "join" or mt == "presence":
                other_id = msg.get("id")
                
                # 如果我還沒決定角色，就來比大小
                if self.role == "UNKNOWN" and other_id != self.my_id:
                    if self.my_id > other_id:
                        self.role = "P1" # ID 大的是 P1
                        self.my_turn = True
                        self.answer = random.randint(1, 99)
                        print(f"\n[系統] 判定為 P1 (Host)。密碼: {self.answer}")
                        # 廣播密碼給 P2 (Demo用)
                        send_json(self.sock, {"type": "sync_ans", "ans": self.answer, "id": self.my_id})
                    else:
                        self.role = "P2" # ID 小的是 P2
                        self.my_turn = False
                        # 回傳一個 presence 讓對方知道我也在 (避免 P1 先進來沒看到 P2)
                        send_json(self.sock, {"type": "presence", "id": self.my_id})
                        print(f"\n[系統] 判定為 P2 (Guest)。等待 P1 設定密碼...")

            # === P2 收到 P1 同步的密碼 ===
            elif mt == "sync_ans":
                # 只有當我還沒設定答案，或是確定對方是 P1 時才接受
                if self.answer is None:
                    self.role = "P2"
                    self.answer = msg.get("ans")
                    self.my_turn = False
                    print(f"\n[系統] 遊戲開始！範圍 0~100")

            # === 收到猜測 ===
            elif mt == "guess":
                num = msg.get("num")
                print(f"\n>> 對手猜了: {num}")
                if num == self.answer:
                    print(">> 對手猜中了！你贏了！")
                    self.running = False
                    input("按 Enter 結束")
                    os._exit(0)
                else:
                    self._update_range(num)
                    self.my_turn = True
                    print(f"範圍更新: {self.min_val} ~ {self.max_val}")
                    print("換你了！")

    # (保留原本的 _update_range 和 _game_loop 不變)
    def _update_range(self, num):
        if num > self.min_val and num < self.answer:
            self.min_val = num
        elif num < self.max_val and num > self.answer:
            self.max_val = num

    def _game_loop(self):
        while self.running:
            if self.my_turn and self.role != "UNKNOWN" and self.answer is not None:
                try:
                    # 使用 flush 確保提示出現
                    print(f"請輸入數字 ({self.min_val}-{self.max_val}): ", end='', flush=True)
                    g = int(input()) # 這裡的 input 容易跟 Lobby 衝突 (見修正步驟 2)
                    
                    if not (self.min_val < g < self.max_val):
                        print("無效範圍")
                        continue
                    
                    send_json(self.sock, {"type": "guess", "num": g})
                    if g == self.answer:
                        print("BOOM! 你猜中了！你輸了！")
                        self.running = False
                        break
                    
                    self._update_range(g)
                    print("等待對手...")
                    self.my_turn = False
                except ValueError: pass
            else:
                time.sleep(0.5)

# (main 區塊保持不變)
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python main.py <host> <port>")
        sys.exit(1)
    
    host, port = sys.argv[1], sys.argv[2]
    game = UltimatePasswordShared(host, port)
    game.start()