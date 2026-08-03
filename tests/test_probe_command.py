"""probe-dataset コマンドのテスト。"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from src.main import _cmd_probe_dataset


def _sample(tmp_path: Path) -> Path:
    records = [
        {"company_name": "A社", "country_code": "JP", "description": "TEL 03-1234-5678",
         "website": "https://a.co.jp"},
        {"company_name": "B社", "country_code": "JP", "description": "電話の記載なし",
         "website": "https://b.co.jp"},
        {"company_name": "C社", "country_code": "JP", "description": "TEL 06-9999-8888",
         "website": None},
        {"company_name": "US Inc", "country_code": "US", "description": "call us"},
    ]
    path = tmp_path / "sample.ndjson"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def _args(path: Path, **overrides) -> argparse.Namespace:
    defaults = dict(
        input=str(path), record_price=0.0025, jpy=150.0,
        contact_price=None, contact_success_rate=0.7, target=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_probe_dataset_reports_measured_rates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _cmd_probe_dataset(_args(_sample(tmp_path)), logging.getLogger(__name__))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "4" in out  # 総レコード数
    assert "日本" in out
    assert "66.7%" in out or "66.7" in out  # 電話率 2/3


def test_probe_dataset_projects_target_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _cmd_probe_dataset(
        _args(_sample(tmp_path), target=41688), logging.getLogger(__name__)
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "41,688" in out


def test_probe_dataset_missing_file_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _cmd_probe_dataset(
        _args(tmp_path / "missing.ndjson"), logging.getLogger(__name__)
    )

    assert exit_code == 1
