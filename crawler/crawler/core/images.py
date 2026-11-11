"""主图下载（带平台 Referer）与本地路径。

图片失败不影响商品行：image_path=null + flag ``主图下载失败``。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from crawler.core.jsonl import relative_or_absolute
from crawler.core.parse import safe_filename

REFERERS = {
    "taobao": "https://s.taobao.com/",
    "tmall": "https://list.tmall.com/",
    "jd": "https://search.jd.com/",
    "1688": "https://s.1688.com/",
}
_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


@dataclass
class HttpResult:
    status: int
    content: bytes
    content_type: str = ""


def _requests_get(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> HttpResult:
    import requests  # 延迟导入，离线解析测试不需要

    response = requests.get(url, headers=headers, timeout=timeout, stream=True)
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            response.close()
            return HttpResult(response.status_code, b"", content_type)
        chunks.append(chunk)
    response.close()
    return HttpResult(response.status_code, b"".join(chunks), content_type)


class ImageDownloader:
    def __init__(
        self,
        root: str | Path,
        platform: str,
        *,
        max_bytes: int = 15 * 1024 * 1024,
        timeout: float = 20.0,
        http_get: Callable[[str, dict[str, str], float, int], HttpResult] | None = None,
        base_dir: str | Path | None = None,
    ):
        self.root = Path(root)
        self.platform = platform
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.http_get = http_get or _requests_get
        self.base_dir = Path(base_dir or Path.cwd())

    def download(self, url: str | None, product_id: str) -> tuple[str | None, str | None]:
        """返回 (本地路径, 失败原因)；成功时失败原因为 None。"""
        if not url:
            return None, "无主图 URL"
        directory = self.root / safe_filename(self.platform)
        directory.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(product_id, "item")
        existing = list(directory.glob(stem + ".*"))
        if existing:
            return relative_or_absolute(existing[0], self.base_dir), None
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": REFERERS.get(self.platform, "https://www.baidu.com/"),
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
        }
        try:
            result = self.http_get(url, headers, self.timeout, self.max_bytes)
        except Exception as exc:  # noqa: BLE001 - 图片失败降级
            return None, f"{type(exc).__name__}: {exc}"
        if result.status != 200:
            return None, f"HTTP {result.status}"
        if not result.content:
            return None, "空响应或超过大小上限"
        content_type = (result.content_type or "").lower()
        if content_type and not content_type.startswith("image/"):
            return None, f"非图片 content-type: {content_type}"
        ext = _EXTENSIONS.get(content_type)
        if not ext:
            suffix = Path(url.split("?", 1)[0]).suffix.lower()
            ext = suffix if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif") else ".jpg"
        path = directory / f"{stem}{ext}"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{stem}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(result.content)
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return relative_or_absolute(path, self.base_dir), None
