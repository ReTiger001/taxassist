"""HTTP 客户端：限速、重试、出网守卫。

**出网守卫（OutboundGuard）是本项目"客户数据不出本机"承诺的技术兜底**：
任何出网请求的 URL 与参数会被逐字符检查，命中客户标识词（税号、客户名等）
直接抛异常并拒绝发送。它拦住的是最常见也最致命的失误——
把客户名或金额误拼进检索关键词里发到公网。

它拦不住的事必须靠流程：不要用本地 LLM 去"总结后再检索"含客户信息的文本。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

import httpx

from ..config import (
    FORBIDDEN_EXTRA_FILE,
    FORBIDDEN_OUTBOUND_PATTERNS,
    MAX_RETRIES,
    REQUEST_INTERVAL_SEC,
    REQUEST_TIMEOUT_SEC,
    RETRY_BACKOFF_SEC,
    USER_AGENT,
)

log = logging.getLogger(__name__)


class OutboundGuardError(RuntimeError):
    """出网请求命中禁用词，已阻止发送。"""


def load_denylist() -> list[str]:
    """内置禁词 + 本地追加禁词（每行一个，本地文件不存在则忽略）。"""
    words = [w for w in FORBIDDEN_OUTBOUND_PATTERNS if w]
    try:
        if FORBIDDEN_EXTRA_FILE.exists():
            for line in FORBIDDEN_EXTRA_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    words.append(line)
    except OSError as e:  # 读不到禁词表不能静默放过
        log.warning("读取本地禁词表失败（将只用内置禁词）: %s", e)
    return words


class GuardedClient:
    """带出网守卫与礼貌限速的 HTTP 客户端。

    用法::

        with GuardedClient() as c:
            data = c.get_json(url, params={...})
    """

    def __init__(
        self,
        interval: float = REQUEST_INTERVAL_SEC,
        timeout: float = REQUEST_TIMEOUT_SEC,
        retries: int = MAX_RETRIES,
        denylist: list[str] | None = None,
    ) -> None:
        self.interval = interval
        self.retries = retries
        self.denylist = denylist if denylist is not None else load_denylist()
        self._client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "application/json, text/html, */*",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        self._last_host_request: dict[str, float] = {}

    # ------------------------------------------------------------ 守卫

    def _check_outbound(self, url: str, params: dict | None, payload: str | None) -> None:
        blob = url
        if params:
            blob += " " + urlencode(params, doseq=True)
        if payload:
            blob += " " + payload
        for word in self.denylist:
            if word and word in blob:
                raise OutboundGuardError(
                    f"出网请求命中禁用词 {word!r}，已阻止发送：{url}"
                    "  —— 若确属误报，请把该词从 outbound_denylist.txt 移除或调整请求内容。"
                )

    # ------------------------------------------------------------ 限速

    def _throttle(self, url: str) -> None:
        from urllib.parse import urlparse

        host = urlparse(url).netloc
        last = self._last_host_request.get(host)
        if last is not None:
            wait = self.interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_host_request[host] = time.monotonic()

    # ------------------------------------------------------------ 请求

    def get(
        self,
        url: str,
        params: dict | None = None,
        *,
        headers: dict | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        """GET 请求，带限速与重试。4xx 不重试（重试无意义），5xx/网络错误重试。"""
        self._check_outbound(url, params, None)
        retries = self.retries if max_retries is None else max_retries
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle(url)
            try:
                resp = self._client.get(url, params=params, headers=headers)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"服务端错误 {resp.status_code}", request=resp.request, response=resp
                    )
                return resp
            except Exception as e:  # noqa: BLE001 - 需要统一重试判定
                last_err = e
                if isinstance(e, httpx.HTTPStatusError) and e.response is not None \
                        and 400 <= e.response.status_code < 500:
                    raise
                if attempt < retries:
                    backoff = RETRY_BACKOFF_SEC * (attempt + 1)
                    log.warning("请求失败（第 %d 次），%.1fs 后重试：%s", attempt + 1, backoff, e)
                    time.sleep(backoff)
        raise RuntimeError(f"请求最终失败: {url} -> {last_err}")

    def get_json(self, url: str, params: dict | None = None, **kw) -> dict:
        resp = self.get(url, params, **kw)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GuardedClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
