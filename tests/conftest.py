import threading

import pytest

from mockbank import server


@pytest.fixture(scope="session")
def base():
    srv = server.make_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
