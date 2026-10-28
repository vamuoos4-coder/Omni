"""Start the loopback web server and choose pywebview or the system browser."""
import argparse
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from server import create_app


def _free_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(port, token, timeout=10):
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    request = urllib.request.Request(url, headers={"X-Token": token})
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    return False


def _request_stop(port, token):
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/stop", data=b"{}", method="POST",
            headers={"X-Token": token, "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{port}"})
        with urllib.request.urlopen(request, timeout=1):
            pass
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Launch Omni Agent in a local web window.")
    parser.add_argument("--browser", action="store_true", help="Force the default browser instead of pywebview.")
    args = parser.parse_args(argv)

    port = _free_loopback_port()
    requested_mode = "browser" if args.browser else "webview"
    app = create_app(port=port, window_mode=requested_mode)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, name="omni-web-server", daemon=True)
    server_thread.start()
    token = app.state.session_token
    if not _wait_for_health(port, token, timeout=10):
        print("Omni web server did not become healthy within 10 seconds; check the console for errors.")
        server.should_exit = True
        server_thread.join(timeout=2)
        return 1

    url = f"http://127.0.0.1:{port}/?t={token}"

    def close_server():
        _request_stop(port, token)
        server.should_exit = True

    if args.browser:
        print("Browser mode requested. Opening the local Omni page; press Ctrl+C here to stop the server.")
        webbrowser.open(url, new=2)
        try:
            while server_thread.is_alive():
                server_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            close_server()
        return 0

    try:
        import webview
    except Exception as exc:
        print(f"pywebview is unavailable ({exc}); opening Omni in your default browser instead.")
        app.state.window_mode = "browser"
        webbrowser.open(url, new=2)
        try:
            while server_thread.is_alive():
                server_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            close_server()
        return 0

    try:
        window = webview.create_window("Omni Agent", url, width=1200, height=800,
                                       min_size=(900, 600))
        window.events.closed += close_server
        webview.start()
        close_server()
        server_thread.join(timeout=3)
        return 0
    except Exception as exc:
        print(f"The Omni webview could not be opened ({exc}); opening the default browser instead.")
        app.state.window_mode = "browser"
        webbrowser.open(url, new=2)
        try:
            while server_thread.is_alive():
                server_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            close_server()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
