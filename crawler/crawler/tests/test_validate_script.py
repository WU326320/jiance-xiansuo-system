"""11/6 e 包：wu_validate.ps1 的路径、标题后缀与奥特曼双口径接线。"""

from __future__ import annotations

from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "wu_validate.ps1"


@pytest.mark.skipif(not SCRIPT.is_file(), reason="wu_validate.ps1 不在测试上下文中")
def test_wu_validate_uses_e_paths_and_package_derived_title():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    for expected in (
        "$script:LogRel = 'logs\\validate_' + $script:ValidateVersion",
        "$script:OutRel = 'out\\validate_' + $script:ValidateVersion",
        "$script:BackupRel = '_backup_' + $script:ValidateVersion",
        "code_20261106e.zip",
        "--run-date",
    ):
        assert expected in text
    # 标题日期后缀必须从包名派生，不能像 c 轮那样写死。
    assert "$script:TitleVersion" in text
    assert "WU 一次性验证摘要（2026-11-06c）" not in text
    assert "WU 一次性验证摘要（2026-11-06e）" not in text
    assert "code_20261106b.zip" not in text
    assert "--compare-dir" not in text
    assert "shop_id_consistency.md" not in text
    assert "同店跨运行 shop_id 一致性" not in text
    assert "店铺名非空率" in text
    assert "候选店名非空率" in text
    assert "run_logs" in text
    assert "crawler\\.venv\\Scripts\\python.exe" in text


def test_wu_validate_offline_jd_near_band_is_empty_interval():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "${1}2.0" in text
    assert "${1}1.0" in text
    assert "2.0-1.0 空区间" in text
    # 旧的非空临界带会让 dry-run 触发京东补抓并联网。
    assert "京东临界带 2.0-3.0" not in text


def test_package_dist_never_freezes_backend_or_export():
    package = SCRIPT.parent / "wu_tools" / "package_dist.py"
    text = package.read_text(encoding="utf-8")
    assert "FROZEN_PREFIXES" not in text
    assert "crawler/crawler/platforms/taobao.py" in text
    assert "crawler/config/rules.yaml" in text
    assert "crawler/config/windows.yaml" in text
    assert "backend/pipeline/views.py" in text
    assert "export/lead_excel/workbook.py" in text
    assert "crawler/crawler/platforms/ali1688.py" in text
    assert "wu_tools/ultraman_report.py" in text
    assert "code_20261106e.zip" in text
    assert "all_md5_match" in text
