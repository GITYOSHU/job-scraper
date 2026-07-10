"""CSV ファイル書き込みモジュール。

Google Sheets を使わずローカルで完結させたい場合のデフォルト出力形式。
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

from .models import JobPosting, SHEET_HEADER

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


class CsvWriter:
    """求人データを CSV ファイルに書き出すクラス。

    Args:
        output_dir: 出力先ディレクトリ (デフォルト: ./output)
        filename: 出力ファイル名。None の場合はタイムスタンプで自動生成
    """

    def __init__(
        self,
        output_dir: str | Path = "output",
        filename: str | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.filename = filename

    def _resolve_path(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.filename:
            return self.output_dir / self.filename
        timestamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
        return self.output_dir / f"jobs-{timestamp}.csv"

    def write(self, postings: Iterable[JobPosting]) -> tuple[Path, int]:
        """求人データを CSV に書き込む。

        Returns:
            (書き込んだファイルパス, 書き込んだ行数)
        """
        path = self._resolve_path()
        rows = [posting.to_row() for posting in postings]

        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(SHEET_HEADER)
            writer.writerows(rows)

        logger.info(f"{len(rows)} 件を CSV に書き出しました: {path}")
        return path, len(rows)
