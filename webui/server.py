"""本地 Web 前端 —— 输入 AppID 入库

只用 Python 标准库（零依赖）。绑定 127.0.0.1，不对局域网开放。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from webui_core import (search, install, uninstall, fetch_dlcs, inventory,  # noqa: E402
                        update_check_all, update_apply, prewarm_key_tables)

HOST = "127.0.0.1"
PORT = 8917

# 同一时间只允许一个写操作（避免并发写坏配置）
_lock = threading.Lock()
_last: dict = {"op": None, "ts": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静音
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器已断开（刷新 / 关页面 / 等太久）：结果没人收，安静放弃。
            # 不能让它冒泡 —— 否则会被端点的 except 误报成"检查更新失败"，
            # 再往已关闭的连接写 500，又一次 BrokenPipe，日志刷满 traceback
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            idx = Path(__file__).parent / "index.html"
            self._send(200, idx.read_bytes(), "text/html")
            return

        if u.path == "/api/update/check":
            try:
                self._json({"updates": update_check_all()})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"检查更新失败: {str(exc)[:200]}"}, 500)
            return

        if u.path == "/api/update/apply":
            raw = (q.get("appid") or [""])[0].strip()
            if not raw.isdigit():
                self._json({"error": "请输入纯数字的 AppID"}, 400)
                return
            if not _lock.acquire(blocking=False):
                self._json({"error": "有另一个操作正在进行，请稍候"}, 429)
                return
            try:
                self._json(update_apply(int(raw)))
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"更新失败: {str(exc)[:200]}"}, 500)
            finally:
                _lock.release()
            return

        if u.path == "/api/list":
            try:
                self._json({"games": inventory()})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"读取列表失败: {str(exc)[:200]}"}, 500)
            return

        if u.path == "/api/search":
            raw = (q.get("appid") or [""])[0].strip()
            if not raw.isdigit():
                self._json({"error": "请输入纯数字的 AppID"}, 400)
                return
            appid = int(raw)
            if appid <= 0:
                self._json({"error": "AppID 必须大于 0"}, 400)
                return
            try:
                res = search(appid)
                if res.get("found"):
                    try:
                        res["dlc_count"] = len(fetch_dlcs(appid))
                    except Exception:
                        res["dlc_count"] = 0
                self._json(res)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"搜索失败: {str(exc)[:200]}"}, 500)
            return

        if u.path == "/api/install":
            raw = (q.get("appid") or [""])[0].strip()
            if not raw.isdigit():
                self._json({"error": "请输入纯数字的 AppID"}, 400)
                return
            appid = int(raw)
            inc = (q.get("dlc") or ["0"])[0] in ("1", "true", "yes")
            if not _lock.acquire(blocking=False):
                self._json({"error": "有另一个操作正在进行，请稍候"}, 429)
                return
            try:
                _last["op"], _last["ts"] = f"install:{appid}", time.time()
                self._json(install(appid, include_dlc=inc))
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"入库失败: {str(exc)[:200]}"}, 500)
            finally:
                _lock.release()
            return

        if u.path == "/api/uninstall":
            raw = (q.get("appid") or [""])[0].strip()
            if not raw.isdigit():
                self._json({"error": "请输入纯数字的 AppID"}, 400)
                return
            appid = int(raw)
            if not _lock.acquire(blocking=False):
                self._json({"error": "有另一个操作正在进行，请稍候"}, 429)
                return
            loc = (q.get("local") or ["0"])[0] in ("1", "true", "yes")
            try:
                self._json(uninstall(appid, remove_local=loc))
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"卸载失败: {str(exc)[:200]}"}, 500)
            finally:
                _lock.release()
            return

        self._json({"error": "not found"}, 404)


def main() -> None:
    url = f"http://{HOST}:{PORT}/"
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        # 端口被占：说明已经有一个实例在跑，直接开浏览器就好
        if getattr(exc, "errno", None) in (48, 98) or "in use" in str(exc).lower():
            print(f"  服务已在运行，打开 {url}", flush=True)
            try:
                webbrowser.open(url)
            except Exception:
                pass
            return
        print(f"  启动失败: {exc}", flush=True)
        return
    print(f"  前端已启动: {url}", flush=True)
    threading.Thread(target=prewarm_key_tables, name="prewarm", daemon=True).start()
    print("  按 Ctrl+C 停止", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止", flush=True)
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
