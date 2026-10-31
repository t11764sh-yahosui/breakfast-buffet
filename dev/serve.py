"""開発用サーバー(ライブリロード付き)

    python3 dev/serve.py [--port 8000]

プロジェクト直下を配信し、HTML には「ファイルが変わったら再読み込みする」
小さなスクリプトを差し込む(index.html 自体は書き換えない)。
"""
import argparse
import functools
import http.server
import os
import socketserver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH_EXT = ('.html', '.css', '.js')

RELOAD_SNIPPET = """
<script>
/* dev/serve.py が差し込むライブリロード(本番には含まれない) */
(() => {
  let last = null;
  setInterval(async () => {
    try {
      const v = await (await fetch('/__version', { cache: 'no-store' })).text();
      if (last !== null && v !== last) location.reload();
      last = v;
    } catch (_) {}
  }, 600);
})();
</script>
""".encode()


def latest_mtime():
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in ('test-results', '__pycache__')]
        for name in filenames:
            if name.endswith(WATCH_EXT):
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
    return newest


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def do_GET(self):
        if self.path == '/__version':
            body = repr(latest_mtime()).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            path = os.path.join(path, 'index.html')
        if path.endswith('.html') and os.path.isfile(path):
            with open(path, 'rb') as f:
                body = f.read().replace(b'</body>', RELOAD_SNIPPET + b'</body>', 1)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        if '/__version' not in str(args[0] if args else ''):
            super().log_message(fmt, *args)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(port):
    return Server(('127.0.0.1', port), functools.partial(Handler, directory=ROOT))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8000)
    args = ap.parse_args()
    srv = make_server(args.port)
    print(f'http://localhost:{args.port}/ で配信中(ファイルを保存するとブラウザが自動で再読み込みされます)')
    srv.serve_forever()
