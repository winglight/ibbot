from __future__ import annotations

import json
from src.ibbot.logging import get_log_file, get_logger, read_log_segment


def test_text_logging_and_reading(capsys):
    module_name = "tests.foundation.sample"
    log_file = get_log_file(module_name)
    if log_file.exists():
        log_file.unlink()

    logger = get_logger(module_name, format_type="text")
    logger.info("hello world")

    captured = capsys.readouterr()
    assert "hello world" in captured.out

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "hello world" in content

    first_read, offset = read_log_segment(module_name, offset=0, size=1024)
    assert "hello world" in first_read

    second_read, new_offset = read_log_segment(module_name, offset=offset, size=1024)
    assert second_read == ""
    assert new_offset == offset


def test_json_logging(tmp_path, capsys):
    module_name = "tests.foundation.json_sample"
    custom_dir = tmp_path / "logs"

    logger = get_logger(module_name, format_type="json", log_dir=custom_dir)
    logger.error("oops", extra={"detail": "value"})

    captured = capsys.readouterr()
    assert captured.out.strip(), "Console output should not be empty"

    log_file = get_log_file(module_name, log_dir=custom_dir)
    assert log_file.exists()

    raw_entry = log_file.read_text(encoding="utf-8").strip()
    assert raw_entry, "Log file should contain data"

    record = json.loads(raw_entry)
    assert record["level"] == "ERROR"
    assert record["name"] == module_name
    assert record["message"] == "oops"
    assert "detail" not in record
