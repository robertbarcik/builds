"""Launch the AI Customers Workshop app."""

import webbrowser
import threading
import uvicorn

from app.config import PORT


def open_browser():
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    threading.Timer(1.5, open_browser).start()
    print(f"\n  AI Customers Workshop → http://localhost:{PORT}\n")
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, log_level="info")
