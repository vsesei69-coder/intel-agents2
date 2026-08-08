"""Browser Proxy — routes API calls through Playwright to bypass AgentRouter client detection.

Uses real Chrome fingerprint (TLS, headers, JS context).
AgentRouter sees a browser, not a Node.js HTTP client.

Usage:
    python browser_proxy.py --port 8787
    Then set OpenCode provider baseURL to http://127.0.0.1:8787/v1
"""

import argparse, json, sys, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("pip install playwright && playwright install chromium", file=sys.stderr)
    sys.exit(1)

TARGET = "https://agentrouter.org"
PAGE = None
BROWSER = None
PLAYWRIGHT = None
LOCK = threading.Lock()


def init_browser():
    global PLAYWRIGHT, BROWSER, PAGE
    PLAYWRIGHT = sync_playwright().start()
    BROWSER = PLAYWRIGHT.chromium.launch(headless=True)
    PAGE = BROWSER.new_page()
    # Warm up: visit AgentRouter to establish trusted session
    try:
        PAGE.goto("https://agentrouter.org", wait_until="domcontentloaded", timeout=15000)
        time.sleep(1)
    except Exception:
        pass


def proxy_request(method, path, body, headers):
    """Forward request through browser's fetch API."""
    with LOCK:
        url = f"{TARGET}{path}"
        js = f"""
        async () => {{
            const headers = {json.dumps(headers)};
            const resp = await fetch({json.dumps(url)}, {{
                method: {json.dumps(method)},
                headers: headers,
                body: {json.dumps(body) if body else 'undefined'},
            }});
            const text = await resp.text();
            return {{
                status: resp.status,
                statusText: resp.statusText,
                headers: Object.fromEntries(resp.headers.entries()),
                body: text
            }};
        }}
        """
        try:
            result = PAGE.evaluate(js)
            return result
        except Exception as e:
            return {"status": 502, "statusText": "Proxy Error",
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps({"error": str(e)})}


class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")

    def _handle(self, method):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else None

        # Forward headers
        fwd_headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length", "connection"):
                fwd_headers[k] = v

        result = proxy_request(method, self.path, body, fwd_headers)

        self.send_response(result["status"])
        for k, v in result.get("headers", {}).items():
            if k.lower() not in ("content-encoding", "transfer-encoding"):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(result["body"].encode("utf-8"))

    def log_message(self, format, *args):
        pass  # suppress logs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    print(f"[*] Starting browser proxy on http://127.0.0.1:{args.port}")
    print(f"[*] Target: {TARGET}")
    print(f"[*] Starting Chromium...")
    init_browser()
    print(f"[*] Browser ready. Accepting requests.")
    print(f"\n    Set OpenCode baseURL to: http://127.0.0.1:{args.port}/v1\n")

    server = HTTPServer(("127.0.0.1", args.port), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        BROWSER.close()
        PLAYWRIGHT.stop()
        print("\n[*] Proxy stopped.")


if __name__ == "__main__":
    main()
