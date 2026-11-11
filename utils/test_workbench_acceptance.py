"""验收入口的参数传递测试，隔离正式数据库和浏览器。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from utils import workbench_acceptance as entry


@pytest.mark.parametrize("background", [False, True])
def test_deadline_forwarded_without_changing_platforms(
    tmp_path, monkeypatch, background
):
    from workbench.config import WorkbenchConfig
    import workbench.db
    import workbench.tasks

    deadline = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    config = SimpleNamespace(
        db_path=tmp_path / "isolated.db",
        logs_dir=tmp_path,
        runs_dir=tmp_path,
        ensure_dirs=lambda: None,
    )
    monkeypatch.setattr(WorkbenchConfig, "load", lambda: config)
    db = Mock()
    db.fetchone.side_effect = [None, {"id": 1, "role": "supervisor"}]
    monkeypatch.setattr(workbench.db, "Database", lambda path: db)
    monkeypatch.setattr(entry, "replace", lambda config, **kw: config)
    service = Mock()
    service.create_task.return_value = {
        "task_no": "test",
        "status": "queued",
        "ends_at": deadline,
    }
    service.execute.return_value = {"status": "success"}
    (tmp_path / "test").mkdir()
    monkeypatch.setattr(workbench.tasks, "TaskService", lambda *args: service)
    popen = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(entry.subprocess, "Popen", popen)
    argv = ["acceptance", "--ends-at", deadline, "--platforms", "taobao", "jd", "1688"]
    if background:
        argv.append("--background")
    monkeypatch.setattr(entry.sys, "argv", argv)
    assert entry.main() == 0
    if background:
        command = popen.call_args.args[0]
        assert command[command.index("--ends-at") + 1] == deadline
        assert command[command.index("--platforms") + 1 :] == ["taobao", "jd", "1688"]
        service.create_task.assert_not_called()
    else:
        payload = service.create_task.call_args.args[0]
        assert payload["ends_at"] == deadline
        assert payload["platforms"] == ["taobao", "jd", "1688"]
        assert service.create_task.call_args.kwargs["run_now"] is False
        service.execute.assert_called_once_with("test")


@pytest.mark.parametrize("args", [[], ["--ends-at", "2000-01-01 00:00:00"]])
def test_missing_or_expired_deadline_rejected_before_database(monkeypatch, args):
    from workbench.config import WorkbenchConfig

    monkeypatch.setattr(entry.sys, "argv", ["acceptance", *args])
    load = Mock(side_effect=AssertionError("不得访问正式配置或数据库"))
    monkeypatch.setattr(WorkbenchConfig, "load", load)
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 2
    load.assert_not_called()
