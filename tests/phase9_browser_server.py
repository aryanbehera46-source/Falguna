import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.runtime import open_control_plane
from falguna.web import FalgunaHandler


temp = tempfile.TemporaryDirectory()
root = Path(temp.name) / "repo"
root.mkdir()
# Complete the additive migration once before the threaded HTTP server starts;
# this prevents two first-load requests racing on schema initialization.
control, store = open_control_plane(root)
store.close()
server = ThreadingHTTPServer(("127.0.0.1", 8883), FalgunaHandler)
server.app_root = root
try:
    server.serve_forever()
finally:
    server.server_close()
    temp.cleanup()
