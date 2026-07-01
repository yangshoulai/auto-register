from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from pydoll.browser.chromium.base import Browser
from pydoll.browser.chromium.chrome import Chrome
from pydoll.browser.tab import Tab

from register.register_flow import RegisterContext

logger = logging.getLogger(__name__)

PYDOLL_BROWSER_STATE_KEY = "pydoll_browser"
PYDOLL_INITIAL_TAB_STATE_KEY = "pydoll_initial_tab"
CURRENT_TAB_STATE_KEY = "current_tab"
PYDOLL_BROWSER_CREATED_STATE_KEY = "pydoll_browser_created"

BrowserFactory = Callable[[], Browser]


class PydollBrowserContextInitializer:
    """
    注册流程启动时初始化 pydoll 浏览器上下文。
    """

    def __init__(
        self,
        *,
        browser_factory: BrowserFactory | None = None,
        browser_start_timeout: int = 30,
        browser_state_key: str = PYDOLL_BROWSER_STATE_KEY,
        initial_tab_state_key: str = PYDOLL_INITIAL_TAB_STATE_KEY,
        current_tab_state_key: str = CURRENT_TAB_STATE_KEY,
    ) -> None:
        self._browser_factory = browser_factory or (
            lambda: _create_default_browser(browser_start_timeout)
        )
        self._browser_state_key = browser_state_key
        self._initial_tab_state_key = initial_tab_state_key
        self._current_tab_state_key = current_tab_state_key

    async def initialize(self, ctx: RegisterContext) -> None:
        browser = ctx.get_value(self._browser_state_key)
        browser_created = False
        if browser is None:
            logger.debug("创建 pydoll 浏览器实例")
            browser = self._browser_factory()
            browser_created = True
        else:
            logger.info("复用已有 pydoll 浏览器实例")
        ctx.set_value(self._browser_state_key, browser)
        ctx.set_value(PYDOLL_BROWSER_CREATED_STATE_KEY, browser_created)

        tab = ctx.get_value(self._initial_tab_state_key)
        if tab is None:
            try:
                logger.debug("启动 pydoll 浏览器并初始化 TAB")
                tab = await browser.start()
            except Exception as exc:
                from register.register_flow import RegisterFlowError

                logger.exception("pydoll 浏览器启动失败")
                raise RegisterFlowError(
                    "pydoll 浏览器启动失败，请检查 Chrome 是否可用，"
                    "或调大 browser_start_timeout"
                ) from exc
            logger.debug("pydoll 浏览器启动完成，配置 CDP loopback 地址")
            await _configure_browser_loopback_ws_address(browser)
            _configure_tab_loopback_ws_address(browser, tab)
            await _maximize_browser_window(browser)
        else:
            logger.info("复用已有初始 TAB")

        ctx.set_value(self._initial_tab_state_key, tab)
        ctx.set_value(self._current_tab_state_key, tab)
        logger.debug("浏览器上下文就绪")


def _create_default_browser(browser_start_timeout: int = 30) -> Chrome:
    from pydoll.browser.options import ChromiumOptions
    from pydoll.connection.connection_handler import ConnectionHandler

    options = ChromiumOptions()
    options.headless = False
    options.set_accept_languages("zh-CN,zh;q=0.9")

    options.start_timeout = browser_start_timeout
    browser = Chrome(options=options)
    browser._connection_handler = ConnectionHandler(
        browser._connection_port,
        ws_address_resolver=_get_browser_ws_address_from_loopback,
    )
    return browser


async def _get_browser_ws_address_from_loopback(port: int) -> str:
    errors: list[str] = []
    for host in ("127.0.0.1", "localhost"):
        try:
            return await _fetch_browser_ws_address(port, host)
        except Exception as exc:
            errors.append(f"{host}: {type(exc).__name__}: {exc}")

    raise RuntimeError("; ".join(errors))


async def _fetch_browser_ws_address(port: int, host: str) -> str:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=1)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"http://{host}:{port}/json/version") as response:
            response.raise_for_status()
            data = await response.json()

    ws_address = data["webSocketDebuggerUrl"]
    return _normalize_ws_host(ws_address, host)


def _normalize_ws_host(ws_address: str, host: str) -> str:
    parts = urlsplit(ws_address)
    netloc = host
    if parts.port is not None:
        netloc = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _configure_browser_loopback_ws_address(browser: Browser) -> None:
    port = browser._connection_port
    browser._ws_address = await _get_browser_ws_address_from_loopback(port)
    logger.debug("浏览器 websocket 地址已修正为 loopback: port=%s", port)


def _configure_tab_loopback_ws_address(browser: Browser, tab: Tab) -> None:
    port = browser._connection_port
    target_id = tab._target_id

    from pydoll.connection.connection_handler import ConnectionHandler

    ws_address = _resolve_tab_ws_address(browser, target_id, port)
    tab._ws_address = ws_address
    tab._connection_handler = ConnectionHandler(ws_address=ws_address)
    logger.debug("TAB websocket 地址已修正: target_id=%s", target_id)


def _resolve_tab_ws_address(browser: Browser, target_id: str, port: int) -> str:
    if browser._ws_address:
        return str(browser._get_tab_ws_address(target_id))
    return f"ws://127.0.0.1:{port}/devtools/page/{target_id}"


async def _maximize_browser_window(browser: Browser) -> None:
    await browser.set_window_maximized()
    logger.debug("浏览器窗口已最大化")
