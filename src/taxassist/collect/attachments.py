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
    return "\n".join(parts)


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
    import docx

    d = docx.Document(str(path))
    return "\n".join(p.text for p in d.paragraphs if p.text.strip())


# 老式文档（OLE2 的 .doc/.wps）没有纯 Python 解析器，但本机装有 WPS，
# 它注册了 KWPS.Application（文字）/ KET.Application（表格）两个 COM 类。
# 让 WPS 把文件另存为 .docx，再交给 python-docx —— 质量远高于自己啃 OLE2 二进制。
_WPS_WRITER = "KWPS.Application"
_WD_FORMAT_DOCX = 16          # Word 的「默认文档格式」，在 WPS 上同样接受


def convert_legacy_doc(path: Path) -> Path | None:
    """用 WPS 把老式文档转成 .docx，成功返回新路径，失败返回 None。

    **失败必须返回 None 而不是抛异常**：转换依赖外部程序（可能未启动、可能
    弹窗、可能被安全软件拦），个别文件转不了是常态，不该让整批解析中断。
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
    """下载附件到 dest，返回字节数。"""
    resp = client.get(normalise_url(url))
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return len(resp.content)
