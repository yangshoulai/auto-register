from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from register.register_flow import RegisterFlowError

logger = logging.getLogger(__name__)


class LocalCallbackServer:
    """
    本地 OAuth 回调 HTTP 服务。

    Codex OAuth 重定向地址默认指向 localhost:1455。这里启动一个最小 HTTP 服务，
    让 Chrome 能正常加载回调页，注册流程再从浏览器当前地址读取完整 redirect_url。
    """

    DEFAULT_HOST = "localhost"
    DEFAULT_PORT = 1455

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._host = host
        self._port = port
        self._server: _ReusableThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self) -> None:
        if self._server is not None:
            return

        try:
            self._server = _ReusableThreadingHTTPServer(
                (self._host, self._port),
                _OkCallbackHandler,
            )
        except OSError as exc:
            logger.exception("本地 OAuth 回调服务启动失败: url=%s", self.url)
            raise RegisterFlowError(
                f"本地 OAuth 回调服务启动失败: {self.url}，请检查端口是否被占用"
            ) from exc
        self._port = int(self._server.server_address[1])

        self._thread = Thread(
            target=self._server.serve_forever,
            name="local-oauth-callback-server",
            daemon=True,
        )
        self._thread.start()
        logger.debug("本地 OAuth 回调服务已启动: url=%s", self.url)

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None

        if server is None:
            return

        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        logger.debug("本地 OAuth 回调服务已关闭: url=%s", self.url)


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class _OkCallbackHandler(BaseHTTPRequestHandler):
    RESPONSE_BODY = b"ok"

    def do_GET(self) -> None:
        self._write_ok_response()

    def do_POST(self) -> None:
        self._write_ok_response()

    def do_OPTIONS(self) -> None:
        self._write_ok_response()

    def log_message(self, format: str, *args) -> None:
        return

    def _write_ok_response(self) -> None:
        logger.debug("收到本地 OAuth 回调请求: method=%s, path=%s", self.command, self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(self.RESPONSE_BODY)))
        self.end_headers()
        self.wfile.write(self.RESPONSE_BODY)
