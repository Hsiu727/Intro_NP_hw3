# HW3 Client Usage (Developer + Player)

This README describes how to start servers and use the Developer and Player clients.

## Requirements
- Python 3.9+


## Developer Client (D1/D2/D3)
Start:
```
python dev_client.py
```

Menu flow:
1) Register or Login
2) [D1] Create & Upload new game
   - Provide game name and file path (e.g. `server_games/MyGame/main.py`).
3) [D2] Update game version & upload new file
4) [D3] Set game status
   - Use `PUBLISHED` to make the game visible to players.
   - Use `UNLOADED` or `DISABLED` to hide it.

操作:
- [D1] 選擇上架遊戲 > 輸入遊戲名稱 > 輸入遊戲本地地址
- [D2] 選擇更新遊戲版本 > 輸入遊戲名稱 > 輸入版本號
(預設為v0.0.0)
- [D3]選擇變更遊戲狀態 > 輸入遊戲名稱 > 更改遊戲當前狀態

## Player Client (R1-R4)
Start:
```
python lobby_client.py
```

Main flow:
1) Register or Login
2) Browse Store and download game
3) Create a room with a downloaded game
4) Invite/join another player
5) Start the game
6) Leave rating after download

操作:
- [P1] 選擇瀏覽商城 > 輸入遊戲名稱以查看詳細資訊
- [P2] 選擇瀏覽商城 > 輸入遊戲名稱 > 選擇下載遊戲
- [P3] 選擇建立房間 > 選擇房間遊戲
- [P4] 選擇瀏覽商城 > 輸入遊戲名稱 > 選擇查看評價即可看到已有評價 > 輸入y以評價
