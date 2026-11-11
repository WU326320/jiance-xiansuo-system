from __future__ import annotations

from pathlib import Path

from crawler.core.images import HttpResult, ImageDownloader


def test_download_success_and_reuse(tmp_path):
    calls = []

    def http_get(url, headers, timeout, max_bytes):
        calls.append((url, headers))
        return HttpResult(200, b"jpeg-bytes", "image/jpeg")

    downloader = ImageDownloader(tmp_path / "images", "taobao", http_get=http_get, base_dir=tmp_path)
    path, error = downloader.download("https://img.alicdn.com/a.jpg", "123")
    assert error is None
    assert path and Path(path) == Path("images") / "taobao" / "123.jpg"
    assert (tmp_path / path).read_bytes() == b"jpeg-bytes"
    assert calls[0][1]["Referer"] == "https://s.taobao.com/"
    path2, error2 = downloader.download("https://img.alicdn.com/a.jpg", "123")
    assert path2 == path and error2 is None
    assert len(calls) == 1


def test_download_failures(tmp_path):
    downloader = ImageDownloader(tmp_path, "jd", http_get=lambda *a: HttpResult(404, b"", "image/jpeg"), base_dir=tmp_path)
    path, error = downloader.download("https://img.jd.com/a.jpg", "1")
    assert path is None and "404" in error

    downloader2 = ImageDownloader(tmp_path, "jd", http_get=lambda *a: HttpResult(200, b"<html>", "text/html"), base_dir=tmp_path)
    path2, error2 = downloader2.download("https://img.jd.com/a.jpg", "2")
    assert path2 is None and "content-type" in error2

    downloader3 = ImageDownloader(
        tmp_path,
        "jd",
        http_get=lambda *a: (_ for _ in ()).throw(TimeoutError("slow")),
        base_dir=tmp_path,
    )
    path3, error3 = downloader3.download("https://img.jd.com/a.jpg", "3")
    assert path3 is None and "TimeoutError" in error3


def test_download_without_url(tmp_path):
    downloader = ImageDownloader(tmp_path, "1688", base_dir=tmp_path)
    assert downloader.download(None, "1") == (None, "无主图 URL")
