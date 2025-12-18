import os
import socket
import sys
import time
from typing import Optional

# 請確認 utils.py 中已包含 send_file, recv_file
from utils import send_json, recv_json, gen_req_id, send_file

HOST = os.getenv("DEV_LOBBY_HOST", "140.113.17.11")
PORT = int(os.getenv("DEV_LOBBY_PORT", "18955"))

class DevClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.authed: Optional[str] = None
        self.running = True

    # ---------- 連線與基礎功能 ----------
    def connect(self):
        if self.sock:
            return True
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=None)
            print(f"[System] Connected to Dev Lobby at {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"[Error] Connection failed: {e}")
            return False

    def close(self):
        if self.sock:
            with self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
            self.sock = None
            self.authed = None

    def call(self, payload: dict):
        """發送請求並等待單次回應 (同步模式)"""
        if not self.sock:
            if not self.connect():
                return None
        
        # 附帶 req_id 以便追蹤 (雖然同步模式下 Server 通常依序回傳)
        if "req_id" not in payload:
            payload = dict(payload)
            payload["req_id"] = gen_req_id("dev")
        
        if not send_json(self.sock, payload):
            print("[Error] Failed to send JSON request.")
            return None
            
        return recv_json(self.sock)

    # ---------- 檔案傳輸邏輯 ----------
    def _perform_file_upload(self, gamename: str, filepath: str):
        """處理檔案上傳的完整握手流程"""
        if not os.path.exists(filepath):
            print(f"[Error] 檔案不存在: {filepath}")
            return False
        
        filename = os.path.basename(filepath)
        print(f"[Upload] 準備上傳 {filename} ({os.path.getsize(filepath)} bytes)...")

        # 1. 發送上傳意圖 (Metadata)
        req = {
            "action": "upload_game_file",
            "gamename": gamename,
            "filename": filename,
            "req_id": gen_req_id("up")
        }
        send_json(self.sock, req)

        # 2. 等待 Server 回覆 Ready
        resp = recv_json(self.sock)
        if not resp or resp.get("msg") != "READY_TO_RECV":
            print(f"[Error] Server 拒絕上傳: {resp}")
            return False

        # 3. 開始傳輸二進制資料
        print("[Upload] 傳輸中...")
        if send_file(self.sock, filepath):
            # 4. 等待最終確認
            final_resp = recv_json(self.sock)
            if final_resp and final_resp.get("status") == "OK":
                print("[Success] 檔案上傳成功！")
                return True
            else:
                print(f"[Error] 上傳後 Server 回報錯誤: {final_resp}")
        else:
            print("[Error] 傳輸過程中斷")
        
        return False

    # ---------- 功能流程 (Use Cases) ----------
    def register(self):
        print("\n=== 註冊開發者 ===")
        username = input("帳號: ").strip()
        password = input("密碼: ").strip()
        if not username or not password:
            print("帳號密碼不可為空")
            return
            
        resp = self.call({"action": "register", "username": username, "password": password})
        print(f"註冊結果: {resp.get('msg') if resp else 'No response'}")

    def login(self):
        print("\n=== 開發者登入 ===")
        username = input("帳號: ").strip()
        password = input("密碼: ").strip()
        resp = self.call({"action": "login", "username": username, "password": password})
        
        if resp and resp.get("status") == "OK":
            self.authed = username
            print(f"登入成功！歡迎, {username}")
        else:
            print(f"登入失敗: {resp.get('msg') if resp else 'No response'}")

    def list_games(self):
        if not self.authed:
            print("[!] 請先登入。")
            return
        
        resp = self.call({"action": "list_games"})
        if resp and resp.get("status") == "OK":
            games = resp.get("games", [])
            print(f"\n=== 我的遊戲列表 ({len(games)}) ===")
            print(f"{'Name':<20} {'Status':<12} {'Version':<10}")
            print("-" * 45)
            for g in games:
                print(f"{g['gamename']:<20} {g['status']:<12} {g['latest']:<10}")
            print("-" * 45)
        else:
            print("查詢失敗:", resp)

    def create_game_flow(self):
        """[D1] 上架新遊戲流程 (包含檔案上傳)"""
        if not self.authed:
            print("[!] 請先登入。")
            return

        print("\n=== [D1] 上架新遊戲 (Only .py are allowed)===")
        gamename = input("遊戲名稱 (ID): ").strip()
        filepath = input("遊戲檔案路徑 (例如 ./dist/main.py): ").strip()

        if not gamename or not filepath:
            print("錯誤: 名稱與路徑皆為必填。")
            return
        
        if not os.path.exists(filepath):
            print("錯誤: 找不到指定的檔案。")
            return

        # 1. 先建立遊戲條目 (Metadata)
        print("正在建立遊戲資訊...")
        resp = self.call({"action": "create_game", "gamename": gamename})
        
        if resp and resp.get("status") == "OK":
            print(f"遊戲 '{gamename}' 建立成功，準備上傳檔案...")
            # 2. 自動接續上傳檔案
            self._perform_file_upload(gamename, filepath)
        else:
            print(f"建立失敗: {resp.get('msg') if resp else 'Error'}")

    def update_game_flow(self):
        """[D2] 更新遊戲流程 (包含檔案上傳)"""
        if not self.authed:
            print("[!] 請先登入。")
            return

        print("\n=== [D2] 更新遊戲版本 ===")
        gamename = input("請輸入要更新的遊戲名稱: ").strip()
        version = input("請輸入新版本號 (例: v1.0.1): ").strip()
        filepath = input("新版檔案路徑: ").strip()

        if not (gamename and version and filepath):
            print("所有欄位皆為必填。")
            return

        # 1. 更新版本資訊
        resp = self.call({"action": "update_game", "gamename": gamename, "version": version})
        
        if resp and resp.get("status") == "OK":
            print(f"版本資訊已更新為 {version}，開始上傳檔案...")
            # 2. 上傳實體檔案
            self._perform_file_upload(gamename, filepath)
        else:
            print(f"版本更新失敗: {resp.get('msg') if resp else 'Error'}")

    def set_game_status(self):
        """[D3] 變更遊戲狀態 (上架/下架)"""
        if not self.authed:
            print("[!] 請先登入。")
            return

        print("\n=== [D3] 變更遊戲狀態 ===")
        gamename = input("遊戲名稱: ").strip()
        print("可用狀態: PUBLISHED (上架), UNLOADED (下架), DISABLED (停用)")
        status = input("新狀態: ").strip().upper()

        if status not in ["PUBLISHED", "UNLOADED", "DISABLED"]:
            print("無效的狀態。")
            return

        resp = self.call({"action": "set_game_status", "gamename": gamename, "status": status})
        print(f"結果: {resp.get('msg') if resp else 'Error'}")

    # ---------- 主選單 UI ----------
    def main_menu(self):
        while self.running:
            print("\n" + "="*30)
            print(f" Developer Client | User: {self.authed or 'Guest'}")
            print("="*30)
            
            if not self.authed:
                print("1. 註冊帳號")
                print("2. 登入")
                print("0. 離開")
            else:
                print("1. 查看我的遊戲列表")
                print("2. [D1] 上架新遊戲 (Create & Upload)")
                print("3. [D2] 更新遊戲版本 (Update & Upload)")
                print("4. [D3] 變更遊戲狀態 (上架/下架)")
                print("5. 登出")
                print("0. 離開")

            choice = input("\n請選擇功能: ").strip()

            try:
                if not self.authed:
                    if choice == "1": self.register()
                    elif choice == "2": self.login()
                    elif choice == "0": break
                    else: print("無效選項")
                else:
                    if choice == "1": self.list_games()
                    elif choice == "2": self.create_game_flow()
                    elif choice == "3": self.update_game_flow()
                    elif choice == "4": self.set_game_status()
                    elif choice == "5": 
                        self.authed = None
                        print("已登出。")
                    elif choice == "0": break
                    else: print("無效選項")
            except (ConnectionError, OSError) as e:
                print(f"[Fatal] 連線錯誤: {e}")
                self.close()
                time.sleep(1)

def main():
    client = DevClient(HOST, PORT)
    try:
        client.main_menu()
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        client.close()

if __name__ == "__main__":
    main()