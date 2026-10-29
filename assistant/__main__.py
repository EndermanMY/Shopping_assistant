import os
import socket
import sys
from pathlib import Path

import uvicorn

from .app import ROOT, create_app, lan_ips


def main():
    port = int(os.environ.get("SHOPPING_PORT", "18766"))
    # Check before constructing SQLite/browser resources, so a second launch
    # cannot mark the live instance's running tasks as interrupted.
    probe = socket.socket()
    try:
        probe.bind(("0.0.0.0", port))
    except OSError as error:
        if getattr(error, "winerror", None) == 10013:
            print(f"端口 {port} 无法绑定：Windows 权限或系统保留端口限制。请换用未被系统保留的端口（源码版使用 start.ps1 -Port，便携版设置 SHOPPING_PORT）。")
        else:
            print(f"端口 {port} 无法绑定，请检查是否已被占用。错误：{error}")
        sys.exit(1)
    finally:
        probe.close()
    app = create_app()
    print(f"\n购物小助手  http://127.0.0.1:{port}")
    for ip in lan_ips():
        print(f"局域网访问  http://{ip}:{port}")
    print(f"访问码（仅在本机终端显示）: {app.state.access.code}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False)


if __name__ == "__main__":
    main()
