import threading
import time

import webview

from app import HOST, PORT, app


def run_server():
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    server = threading.Thread(target=run_server, daemon=True)
    server.start()
    time.sleep(1.2)
    webview.create_window(
        "InvestControl Desktop",
        f"http://{HOST}:{PORT}",
        width=1440,
        height=920,
        min_size=(1100, 720),
    )
    webview.start()
