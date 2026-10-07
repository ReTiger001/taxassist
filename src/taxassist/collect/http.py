"""HTTP 客户端：限速、重试、出网守卫。

**出网守卫（OutboundGuard）是本项目"客户数据不出本机"承诺的技术兜底**：
任何出网请求的 URL 与参数会被逐字符检查，命中客户标识词（税号、客户名等）
直接抛异常并拒绝发送。它拦住的是最常见也最致命的失误——
把客户名或金额误拼进检索关键词里发到公网。

它拦不住的事必须靠流程：不要用本地 LLM 去"总结后再检索"含客户信息的文本。
"""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlencode

import httpx

from ..config import (
    CHALLENGE_RETRY_SEC,
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
            # 不走系统代理。两个理由：
            # ① 本机常年开着 Clash（127.0.0.1:7897），默认读环境变量就会把
            #    抓取请求交给第三方节点 —— 与"数据不出本机"的边界相冲突；
            # ② 代理会改内容：实测河北列表页直连 38286 字节、21 条详情链接，
            #    走代理只有 34068 字节、一条都解析不出来，且看起来像"页面改版"。
            # 抓的都是政府公开站点，直连即可达。
            trust_env=False,
        )
        self._last_host_request: dict[str, float] = {}

    # ------------------------------------------------------------ 守卫

    def _check_outbound(self, url: str, params: dict | None, payload: str | None) -> None:
        blob = url
        if params:
            # **编码前后都要查。** urlencode 会把中文变成 %E5%AE%A2%E6%88%B7…
            # 只把编码后的串拿去匹配，等于漏掉"客户名当检索参数"这条最典型的
            # 泄露路径 —— 补测试时正是这样发现守卫对 params 是失效的：
            # 禁词是「客户甲」，而 blob 里只有 q=%E5%AE%A2%E6%88%B7%E7%94%B2。
            blob += " " + " ".join(str(v) for v in params.values())
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
                # **412 要退避重试，其他 4xx 不要。**
                # 412 是挑战页（"你请求太密了"），语义是"稍后再来"，与
                # 403/404（重试无意义）根本不同。实测河北的 TRS 检索页：
                # 连续请求必 412，隔 3 秒仍有 —— 而它一页能给近 1000 条，
                # 放弃重试就等于静默漏掉整页数据，且状态仍记 ok。
                if resp.status_code == 412 and attempt < retries:
                    wait = CHALLENGE_RETRY_SEC * (attempt + 1)
                    log.warning("遇到挑战页（412），%.0f 秒后重试：%s", wait, url)
                    time.sleep(wait)
                    continue
                return resp
            except Exception as e:
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

    def post_json(self, url: str, payload: dict, *,
                  max_retries: int | None = None) -> dict:
        """POST JSON，带出网守卫、限速与重试。

        为什么必须走这里而不是直接 httpx：出网守卫要检查**请求内容**
        （严禁把客户信息发出去），POST 的报文体同样要过这一关 ——
        绕开它等于给"客户数据不出本机"开一个后门。

        重试策略与 get 一致：4xx 不重试（重试无意义），5xx/网络错误退避重试。
        """
        body = json.dumps(payload, ensure_ascii=False)
        self._check_outbound(url, None, body)
        retries = self.retries if max_retries is None else max_retries
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle(url)
            try:
                resp = self._client.post(
                    url, content=body.encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"服务端错误 {resp.status_code}",
                        request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_err = e
                if isinstance(e, httpx.HTTPStatusError) and e.response is not None \
                        and 400 <= e.response.status_code < 500:
                    raise
                if attempt < retries:
                    backoff = RETRY_BACKOFF_SEC * (attempt + 1)
                    log.warning("POST 失败（第 %d 次），%.1fs 后重试：%s",
                                attempt + 1, backoff, e)
                    time.sleep(backoff)
        raise RuntimeError(f"POST 最终失败: {url} -> {last_err}")

    def post_form(self, url: str, form: dict, *,
                  max_retries: int | None = None) -> httpx.Response:
        """POST 表单（application/x-www-form-urlencoded），带出网守卫与限速。

        为什么需要：部分省的政策检索是**表单提交**（山西 /web/search/sx-11400
        要 keywords / cx_title / cx_content 等字段）。与 post_json 同理 ——
        必须走出网守卫，因为表单内容同样可能夹带客户信息。
        """
        body = urlencode(form)
        self._check_outbound(url, None, body)
        retries = self.retries if max_retries is None else max_retries
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle(url)
            try:
                resp = self._client.post(
                    url, content=body.encode("utf-8"),
                    headers={"Content-Type":
                             "application/x-www-form-urlencoded"})
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"服务端错误 {resp.status_code}",
                        request=resp.request, response=resp)
                resp.raise_for_status()
                return resp
            except Exception as e:
                last_err = e
                if isinstance(e, httpx.HTTPStatusError) and e.response is not None \
                        and 400 <= e.response.status_code < 500:
                    raise
                if attempt < retries:
                    backoff = RETRY_BACKOFF_SEC * (attempt + 1)
                    log.warning("POST 表单失败（第 %d 次），%.1fs 后重试：%s",
                                attempt + 1, backoff, e)
                    time.sleep(backoff)
        raise RuntimeError(f"POST 表单最终失败: {url} -> {last_err}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GuardedClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
