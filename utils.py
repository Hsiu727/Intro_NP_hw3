import json
import struct
import time, random, string
from typing import Any, Optional
import os

MAX_LEN = 65536

def gen_room_id():
    return "r" + "".join(random.choices(string.digits, k=4))

def gen_req_id(prefix: str = "r") -> str:
    ts = int(time.time()*1000)
    salt = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))
    return f"{prefix}-{ts}-{salt}"

def send_all(sock, data: bytes) -> bool:
    total = 0
    try:
        while total < len(data):
            sent = sock.send(data[total:])
            if sent == 0:
                return False
            total += sent
        return True
    except Exception:
        return False

def send_json(sock, obj: Any) -> bool:
    try:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if not (0 < len(body) <= MAX_LEN):
            return False
        header = struct.pack("!I", len(body))
        return send_all(sock, header + body)
    except Exception:
        return False

def recv_exact(sock, n: int) -> Optional[bytes]:
    buf = bytearray()
    try:
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)
    except Exception:
        return None

def recv_json(sock) -> Optional[Any]:
    hdr = recv_exact(sock, 4)
    if not hdr:
        return None
    (length,) = struct.unpack("!I", hdr)    #回傳tuple ex.(128,)
    if length <= 0 or length > MAX_LEN:
        return None
    body = recv_exact(sock, length)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None

def with_req_id(payload: dict, req_id: str):
    if req_id:
        payload = dict(payload)
        payload["req_id"] = req_id
    return payload

def ok(msg: Optional[str] = None, req_id: Optional[str] = None,**kw):
    d = {"status": "OK"}
    if msg is not None:
        d["msg"] = msg
    if kw:
        d.update(kw)
    return with_req_id(d, req_id)

def err(msg: Optional[str] = None, req_id: Optional[str] = None, **kw):
    d = {"status": "ERROR", "msg": msg}
    if kw:
        d.update(kw)
    return with_req_id(d, req_id)

def push_event(conn, event:str, **kw):
    payload = {"event":event}
    if kw:
        payload.update(kw)
    send_json(conn, payload)

def send_file(sock, filepath: str) -> bool:
    """
    發送檔案：
    1. 先發送 8 bytes 的檔案大小 (unsigned long long)
    2. 發送檔案內容
    """
    if not os.path.exists(filepath):
        return False

    filesize = os.path.getsize(filepath)
    try:
        # 1. 發送檔案大小 (Big-endian, 8 bytes)
        sock.sendall(struct.pack("!Q", filesize))

        # 2. 發送檔案內容 (分塊讀取，避免記憶體爆掉)
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                sock.sendall(chunk)
        return True
    except Exception as e:
        print(f"[SendFile] Error: {e}")
        return False

def recv_file(sock, dest_path: str) -> bool:
    """
    接收檔案：
    1. 讀取 8 bytes 檔案大小
    2. 接收指定長度的 bytes 並寫入檔案
    """
    try:
        # 1. 接收檔案大小
        header = recv_exact(sock, 8)
        if not header:
            return False
        (filesize,) = struct.unpack("!Q", header)
        
        # 2. 接收內容
        received = 0
        with open(dest_path, 'wb') as f:
            while received < filesize:
                # 計算這次要收多少 (剩餘量 vs 緩衝區大小)
                remains = filesize - received
                chunk_size = min(65536, remains)
                chunk = recv_exact(sock, chunk_size)
                if not chunk:
                    raise ConnectionError("Connection lost during file transfer")
                f.write(chunk)
                received += len(chunk)
        return True
    except Exception as e:
        print(f"[RecvFile] Error: {e}")
        return False