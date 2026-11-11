"""缩略图生成（Pillow）。

只读本地 image_path；不访问网络。统一转 PNG 并保持宽高比，宽约 80px。
生成的 BytesIO 由 openpyxl.drawing.image.Image 持有，直到 save 完成。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

# 防止恶意/异常大图导致内存膨胀（约 4000 万像素）。
_MAX_PIXELS = 40_000_000


@dataclass
class Thumbnail:
    stream: io.BytesIO
    width: int
    height: int


def _expand_path(value: str) -> str:
    # image_path 由后端写入，可能带 %USERPROFILE%（例如测试/迁移数据）。
    expanded = os.path.expandvars(value)
    if expanded == value and "%" in value:
        # os.path.expandvars 在 POSIX 上不展开 %VAR%，补一个简单处理。
        import re

        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            if name.upper() == "USERPROFILE" and name not in os.environ:
                return str(Path.home())
            return os.environ.get(name, match.group(0))

        expanded = re.sub(r"%([^%]+)%", repl, value)
    return expanded


def resolve_image_path(
    raw_path: object,
    *,
    base_dirs: Iterable[Path] = (),
    db_dir: Path | None = None,
) -> Path | None:
    """把 image_path 解析成存在的文件；不存在返回 None。"""

    if raw_path is None:
        return None
    text = str(raw_path).strip()
    if not text:
        return None

    path = Path(_expand_path(text)).expanduser()
    if path.is_absolute():
        return path if path.is_file() else None

    candidates: list[Path] = []
    for base in base_dirs:
        candidates.append(base / path)
    if not candidates:
        if db_dir is not None:
            # 后端把 image_path 存成“后端根目录相对路径”（如 data/images/xx.jpg），
            # 数据库通常在 <root>/data/leads.db，因此同时尝试 db 目录和其上一级。
            candidates.append(db_dir / path)
            candidates.append(db_dir.parent / path)
        candidates.append(Path.cwd() / path)
    # 去重且保持顺序
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def make_thumbnail(
    raw_path: object,
    *,
    base_dirs: Iterable[Path] = (),
    db_dir: Path | None = None,
    target_width: int = 80,
    max_height: int = 120,
) -> tuple[Thumbnail | None, str | None]:
    """生成缩略图。

    返回 ``(Thumbnail, None)`` 或 ``(None, 原因)``；空路径不视为错误。
    """

    if raw_path is None or not str(raw_path).strip():
        return None, None

    path = resolve_image_path(raw_path, base_dirs=base_dirs, db_dir=db_dir)
    if path is None:
        return None, f"文件不存在：{raw_path}"

    try:
        with Image.open(path) as image:
            if image.width * image.height > _MAX_PIXELS:
                return None, f"图片像素过大（{image.width}x{image.height}）：{raw_path}"

            image = ImageOps.exif_transpose(image)
            # thumbnail 只缩小不放大：小图保持原尺寸，避免模糊。
            image.thumbnail((target_width, max_height), Image.Resampling.LANCZOS)
            width, height = image.size

            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                image = image.convert("RGBA")
            else:
                image = image.convert("RGB")

            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            buffer.seek(0)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return None, f"无法读取图片（{exc}）：{raw_path}"
    except Image.DecompressionBombError as exc:  # pragma: no cover - 防御性分支
        return None, f"图片解压异常（{exc}）：{raw_path}"

    if width <= 0 or height <= 0:
        return None, f"图片尺寸异常：{raw_path}"
    return Thumbnail(stream=buffer, width=width, height=height), None
