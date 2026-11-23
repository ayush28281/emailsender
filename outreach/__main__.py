import argparse
import http.client
import json
import sys
import webbrowser
from pathlib import Path

from .server import LocalServer, Workspace, workspace_key
from .settings import load_env


def already_running(port, folder):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        conn.request("GET", "/api/instance")
        reply = conn.getresponse()
        return reply.status == 200 and json.loads(reply.read(1024)) == {
            "app": "Emailsender", "workspace": workspace_key(folder),
        }
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Run the personal Emailsender dashboard and follow-up worker.")
    parser.add_argument("--port", type=int, default=8765, help="local dashboard port (default 8765)")
    parser.add_argument("--data-dir", type=Path, default=Path(".emailsender"), help="folder for your private queue and settings")
    parser.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Choose a port between 0 and 65535.")
    if args.port and already_running(args.port, args.data_dir):
        url = f"http://127.0.0.1:{args.port}/"
        if args.open:
            webbrowser.open(url)
        print(f"Emailsender is already running at {url}")
        return
    load_env(Path(".env"))
    try:
        space = Workspace(args.data_dir)
        server = LocalServer(args.port, space)
    except (OSError, ValueError) as exc:
        sys.exit(f"Could not open Emailsender: {exc}")
    with server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Emailsender is running at {url}\nKeep this process open for automatic sending. Press Ctrl+C to stop.", flush=True)
        space.start()
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            space.close()


if __name__ == "__main__":
    main()
