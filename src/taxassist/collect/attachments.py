"""附件下载与文本解析。

**为什么必须做**：政策正文之外的附件（申报表、填报说明、管理办法）才是
实务里真正拿来干活的东西。只抓正文等于抓了一半。

格式覆盖：

| 格式 | 解析器 | 说明 |
|---|---|---|
| ``.pdf`` | pymupdf | 抽文本层 |
| ``.xlsx`` | openpyxl | 抽所有工作表的单元格文本 |
| ``.xls`` | xlrd | 老格式，税务申报表大量使用 |
| ``.docx`` | python-docx | |
| ``.doc`` / ``.wps`` / ``.et`` | **不支持** | 标记 unsupported，留待人工或后续接 WPS COM |

**扫描件是真实存在的**：内容为空的 PDF 返回 ``no_text_layer`` 而不是报错。
必须区分"这份文件存在但需要 OCR"和"解析器坏了"——前者你打开看一眼就行，
后者需要改代码。把两者混为一谈会让人误以为系统正常工作。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .http import GuardedClient

log = logging.getLogger(__name__)

PARSERS: dict[str, callable] = {}


def _register(exts: list[str]):
    def deco(fn):
        for e in exts:
            PARSERS[e] = fn
        return fn
    return deco


@_register(["pdf"])
def parse_pdf(path: Path) -> str:
    import pymupdf  # PyMuPDF >= 1.24 的正式模块名（fitz 已弃用）

    parts: list[str] = []
    with pymupdf.open(str(path)) as doc:
        for page in doc:
            parts.append(page.get_text())
    text = "\n".join(parts)
    if text.strip():
        return text
    # **无文字层 → 走 OCR。** 扫描件（图片型 PDF）用 get_text() 抽不出任何
    # 文字，此前一律记为 no_text_layer 而作罢 —— 但它们的文字是"看得见、
    # 读不出"，OCR 正好解决这一类。选 RapidOCR（PaddleOCR 模型的 ONNX 版），
    # 中文精度与 PaddleOCR 同级，却只要 onnxruntime，CPU 可跑。
    return _ocr_pdf(path)


def _ocr_pdf(path: Path) -> str:
    """扫描件 PDF：逐页渲染成图后做 OCR。

    只在 ``get_text()`` 完全抽不出文字时调用 —— 有文字层的 PDF 走原生抽取，
    又快又准，不该浪费 OCR 的时间。
    """
    import numpy as np
    import pymupdf

    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    out: list[str] = []
    with pymupdf.open(str(path)) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n)
            if pix.n == 4:            # 带 alpha 的转成 RGB
                arr = arr[:, :, :3]
            result, _elapse = ocr(arr)
            if result:
                out.append("\n".join(r[1] for r in result))
    return "\n".join(out)


@_register(["xlsx", "xlsm"])
def parse_xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    parts: list[str] = []
    wb = load_workbook(str(path), data_only=True, read_only=True)
    try:
        for ws in wb.worksheets:
            parts.append(f"[工作表] {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    parts.append(" | ".join(cells))
    finally:
        wb.close()
    return "\n".join(parts)


@_register(["xls"])
def parse_xls(path: Path) -> str:
    import xlrd

    parts: list[str] = []
    book = xlrd.open_workbook(str(path))
    for sh in book.sheets():
        parts.append(f"[工作表] {sh.name}")
        for r in range(sh.nrows):
            cells = [str(c).strip() for c in sh.row_values(r) if str(c).strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


@_register(["docx"])
def parse_docx(path: Path) -> str:
    """从 .docx 取文本。

    首选 python-docx（段落结构更可靠），失败时退回直接读 ``word/document.xml``。

    **为什么需要退回**：实测 6 条税务局的 .docx 让 python-docx 抛 KeyError
    （文件里缺它期望的某个 XML 部件，但 Word/WPS 能正常打开）。这类文件不该
    判失败 —— 我们只要文本，段落节点 ``<w:p>`` 与文本节点 ``<w:t>`` 就在
    ``word/document.xml`` 里，用标准库 zipfile 读得到，不依赖 python-docx
    对部件完整性的假设。
    """
    try:
        import docx

        d = docx.Document(str(path))
        text = "\n".join(p.text for p in d.paragraphs if p.text.strip())
        if text.strip():
            return text
    except Exception as e:  # noqa: BLE001 - 退回 XML 抽取，见 docstring
        log.info("python-docx 解析失败，退回 XML 抽取 %s: %s", path.name, e)
    return _docx_text_from_xml(path)


def _docx_text_from_xml(path: Path) -> str:
    """直接从 ``word/document.xml`` 抽文本（python-docx 失败时的兜底）。

    **只吞"缺部件"这一种情况**：``KeyError`` 表示这是个合法 zip、只是没有
    ``word/document.xml`` —— 那确实是没有文字层。文件不存在、不是 zip、
    读不了等情况必须**继续抛出**，让 ``_run_parser`` 记成 ``failed:`` ——
    「解析器跑不了」和「没有文字层」在本模块是两种必须区分的东西
    （前者要改代码，后者人打开看一眼就行）。
    """
    import re as _re
    import zipfile

    with zipfile.ZipFile(path) as zf:
        try:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
        except KeyError:
            return ""
    # 段落边界转成换行，再去掉所有标签 —— 表格单元格也是 <w:p> 包着 <w:t>，
    # 所以这样处理不会丢表格里的文字。
    xml = xml.replace("</w:p>", "\n")
    return _re.sub(r"<[^>]+>", "", xml)


# 老式文档（OLE2 的 .doc/.wps）没有纯 Python 解析器，但本机装有 WPS，
# 它注册了 KWPS.Application（文字）/ KET.Application（表格）两个 COM 类。
# 让 WPS 把文件另存为 .docx，再交给 python-docx —— 质量远高于自己啃 OLE2 二进制。
_WPS_WRITER = "KWPS.Application"
_WD_FORMAT_DOCX = 16          # Word 的「默认文档格式」，在 WPS 上同样接受


def convert_legacy_doc(path: Path) -> Path | None:
    """用 WPS 把老式文档转成 .docx，成功返回新路径，失败返回 None。

    **失败必须返回 None 而不是抛异常**：转换依赖外部程序（可能未启动、可能
    弹窗、可能被安全软件拦），个别文件转不了是常态，不该让整批解析中断。

    **本机实测（2026-10）**：WPS 装着（注册表有 ``KWPS.Application``），
    随机抽 40 个 .doc/.wps 转换 **40/40 成功**。但早先单独测过其中一个文件，
    报过 "文档打开失败（Kingsoft WPS, 3010 / -2147352567）" ——
    **同一个文件隔一段时间再测就成功了**。
    推测是 WPS 首次启动/初始化尚未完成就发起转换所致，
    所以偶发失败属正常现象，**重跑即可，不必当代码 bug 去查**。
    """
    if os.environ.get("TAXASSIST_NO_WPS"):
        # 单元测试不该依赖外部程序：启动 WPS 要几秒，还可能弹窗或挂住。
        # conftest 默认设了这个变量；要专门测转换本身时把它清掉即可。
        return None
    try:
        import win32com.client
    except ImportError:
        log.info("未安装 pywin32，跳过 WPS 转换")
        return None

    app = None
    try:
        app = win32com.client.DispatchEx(_WPS_WRITER)
        app.Visible = False
        app.DisplayAlerts = 0
        doc = app.Documents.Open(str(path), ReadOnly=True, AddToRecentFiles=False)
        try:
            out = path.with_suffix(".docx")
            doc.SaveAs2(str(out), FileFormat=_WD_FORMAT_DOCX)
        finally:
            doc.Close(False)
        return out if out.exists() else None
    except Exception as e:  # noqa: BLE001 - 外部程序，任何异常都算"转不了"
        log.warning("WPS 转换失败 %s: %s", path.name, e)
        return None
    finally:
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass


# 文件头魔数。**不能只信扩展名**：实测税务局的附件里，名为 .docx 的文件
# 文件头是 d0cf11e0（OLE2 老式文档），按扩展名交给 python-docx 必然抛
# PackageNotFoundError —— 那不是"解析失败"，是我们选错了工具。
_MAGIC = (
    (b"PK\x03\x04", "zip"),                          # docx / xlsx / pptx
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2"),   # doc / xls / wps / et（老式）
    (b"%PDF", "pdf"),
)
# 扩展名与真实格式不符时的纠正表
_ZIP_ALIASES = {"doc": "docx", "wps": "docx", "xls": "xlsx", "et": "xlsx", "ppt": "pptx"}
# 老式 OLE2 容器里，表格 xlrd 读得了；老式文档（doc/wps）没有解析器
_OLE2_READABLE = {"xls", "et"}


def sniff_kind(path: Path) -> str | None:
    """按文件头判断真实格式；读不出文件时返回 None。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    return None


def _run_parser(parser, path: Path) -> tuple[str | None, str]:
    if parser is None:
        return None, "unsupported"
    try:
        text = (parser(path) or "").strip()
    except Exception as e:  # noqa: BLE001 - 任何解析器异常都要变成状态而不是崩溃
        log.warning("附件解析失败 %s: %s", path.name, e)
        return None, f"failed:{type(e).__name__}"
    if not text:
        return None, "no_text_layer"
    return text, "ok"


@_register(["txt", "csv", "md", "log", "json", "text"])
def parse_text(path: Path) -> str:
    """纯文本直接读。

    **编码必须逐个试**：中文 Windows 上的 .txt/.csv 常是 GBK/GB18030，
    直接按 utf-8 读会乱码甚至抛异常。政务附件里的 CSV 尤其如此。
    顺序是 utf-8-sig → utf-8 → gb18030 → utf-16：前者能读就用前者
    （utf-8 是当下默认，gb18030 能解码绝大多数遗留文本，兜底最后）。

    这个解析器原来是缺的 —— PARSERS 里只有 pdf/xlsx/xls/docx/zip/rar，
    因为**政策附件里几乎没有纯文本**。对话窗口上线后暴露：用户最常传的
    就是 .txt 摘录，结果报"这个格式读不了"（实测 2026-10-06）。
    """
    raw = path.read_bytes()
    if not raw.strip():
        return ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # 全部失败也要给出内容（有乱码好过空白），并在结尾说明
    return raw.decode("utf-8", "replace")


@_register(["zip"])
def parse_zip(path: Path) -> str:
    """解压后逐一解析内部文件并拼接文本。

    **只解一层**、限制条目数与单文件大小：防恶意构造的嵌套包把磁盘写爆。
    内部文件也走 parse_attachment，所以包里装 .doc/.xls/.pdf 都能继续解析
    （.doc 会再经 WPS 转换）。
    """
    import tempfile
    import zipfile

    parts: list[str] = []
    with zipfile.ZipFile(path) as zf, tempfile.TemporaryDirectory() as td:
        for info in zf.infolist()[:20]:
            if info.is_dir() or info.file_size > 20 * 1024 * 1024:
                continue
            name = Path(info.filename).name        # 丢弃路径，防目录穿越
            if not name:
                continue
            target = Path(td) / name
            target.write_bytes(zf.read(info))
            text, _status = parse_attachment(target)
            if text:
                parts.append(f"【{name}】\n{text}")
    return "\n\n".join(parts)


@_register(["rar"])
def parse_rar(path: Path) -> str:
    """.rar 要外部程序：纯 Python 的 rarfile 也只是前端，底层仍需 unrar 二进制。
    本机有 WinRAR 的 UnRAR.exe，直接调它。找不到就抛异常由 _run_parser 记成
    failed:<类型> —— 不静默返回空串冒充"没有文本层"。
    """
    import subprocess
    import tempfile

    exe = Path(r"C:\Program Files\WinRAR\UnRAR.exe")
    if not exe.exists():
        raise RuntimeError("找不到 UnRAR.exe（本机未装 WinRAR）")
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [str(exe), "x", "-y", "-o+", str(path), str(td) + "\\"],
            capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"UnRAR 退出码 {proc.returncode}")
        parts: list[str] = []
        for f in sorted(Path(td).rglob("*"))[:20]:
            if f.is_file() and f.suffix.lower().lstrip(".") in PARSERS:
                text, _status = parse_attachment(f)
                if text:
                    parts.append(f"【{f.name}】\n{text}")
        return "\n\n".join(parts)


def parse_attachment(path: Path) -> tuple[str | None, str]:
    """解析附件，返回 ``(文本, 状态)``。

    状态取值：``ok`` / ``no_text_layer``（扫描件，无文本层）/
    ``unsupported``（格式本身读不了）/ ``failed:<异常类型>``。

    **以文件内容为准，不以扩展名为准**：这些站点的扩展名会说谎，
    按扩展名选解析器会把"读不了"错报成"解析失败"。
    """
    ext = path.suffix.lower().lstrip(".")
    kind = sniff_kind(path)

    if kind == "ole2":
        # 老式容器。表格能读（xlrd）；文档（.doc/.wps）没有纯 Python 解析器，
        # 先用 WPS 另存为 .docx 再解析（见 convert_legacy_doc）。
        # 转换失败仍然如实标 unsupported —— 那是"格式读不了"，不是"文件坏了"。
        if ext in _OLE2_READABLE:
            return _run_parser(parse_xls, path)
        converted = convert_legacy_doc(path)
        if converted is not None:
            try:
                return _run_parser(parse_docx, converted)
            finally:
                try:
                    converted.unlink()      # 转换产物是临时的，不留档
                except OSError:
                    pass
        return None, "unsupported"

    if kind == "zip" and ext in _ZIP_ALIASES:
        # 反向不符：扩展名说老式，内容其实是新式 ZIP
        return _run_parser(PARSERS.get(_ZIP_ALIASES[ext]), path)

    if kind == "pdf" and ext != "pdf":
        return _run_parser(parse_pdf, path)

    return _run_parser(PARSERS.get(ext), path)


def safe_filename(name: str, fallback: str = "attachment") -> str:
    """把附件名转成安全的本地文件名（去掉路径分隔符与危险字符）。"""
    cleaned = "".join(c for c in (name or "") if c not in '\\/:*?"<>|\r\n\t').strip()
    cleaned = cleaned.strip(". ")
    return cleaned[:120] or fallback


def normalise_url(url: str) -> str:
    """修正 URL 里未转义的百分号。

    实测：文件名含"100%"的附件，其 URL 中的 % 未被编码成 %25，
    服务端直接返回 400 Bad Request（3 个附件因此下载失败）。
    只修**孤立的** %（后面不跟两位十六进制），避免把正确的 %E5 之类误改。
    """
    return re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", url)


def download(client: GuardedClient, url: str, dest: Path) -> int:
    """下载附件到 dest，返回字节数。

    先走 httpx（快）；**失败时改走真浏览器兜底**。实测 165 份附件下载失败
    里有 155 份是 HTTP 412 —— 它们的链接同样在加速乐那类 WAF 后面，httpx
    过不了挑战。浏览器的 cookie 能过同一个挑战，代价是慢得多（每条要走一次
    浏览器），所以只在前者失败时才用。
    """
    u = normalise_url(url)
    try:
        resp = client.get(u)
        resp.raise_for_status()
        data = resp.content
    except Exception as e:  # noqa: BLE001 - 失败原因要保留在错误信息里
        from .browser import fetch_bytes

        try:
            data = fetch_bytes(u)
        except Exception as e2:  # noqa: BLE001
            raise RuntimeError(
                f"httpx 失败（{type(e).__name__}: {e}）；"
                f"浏览器兜底也失败（{type(e2).__name__}: {e2}）"
            ) from e

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)
