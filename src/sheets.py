"""Google Sheets へのデータ書き込みモジュール。

Service Account による認証を使用。
事前にスプレッドシートをサービスアカウントのメールアドレスに共有すること。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import gspread
from google.oauth2.service_account import Credentials

from .models import JobPosting, SHEET_HEADER

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


class SheetsWriter:
    """Google Sheets への書き込みを担うクラス。

    Args:
        service_account_path: サービスアカウント JSON のパス
        spreadsheet_id: 書き込み対象スプレッドシートの ID
        worksheet_name: 書き込み対象ワークシート名（存在しなければ作成）
    """

    def __init__(
        self,
        service_account_path: str | Path,
        spreadsheet_id: str,
        worksheet_name: str = "求人リスト",
    ) -> None:
        self.service_account_path = Path(service_account_path)
        self.spreadsheet_id = spreadsheet_id
        self.worksheet_name = worksheet_name
        self._client: gspread.Client | None = None
        self._worksheet: gspread.Worksheet | None = None

    def _connect(self) -> gspread.Worksheet:
        if self._worksheet is not None:
            return self._worksheet

        if not self.service_account_path.exists():
            raise FileNotFoundError(
                f"サービスアカウント JSON が見つかりません: {self.service_account_path}"
            )

        creds = Credentials.from_service_account_file(
            str(self.service_account_path), scopes=SCOPES
        )
        self._client = gspread.authorize(creds)
        spreadsheet = self._client.open_by_key(self.spreadsheet_id)

        try:
            worksheet = spreadsheet.worksheet(self.worksheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=self.worksheet_name, rows=1000, cols=len(SHEET_HEADER)
            )
            worksheet.append_row(SHEET_HEADER, value_input_option="USER_ENTERED")
            logger.info(f"ワークシート '{self.worksheet_name}' を新規作成しました。")

        self._ensure_header(worksheet)
        self._worksheet = worksheet
        return worksheet

    @staticmethod
    def _ensure_header(worksheet: gspread.Worksheet) -> None:
        """先頭行にヘッダーが無ければ追加。"""
        current = worksheet.row_values(1)
        if current != SHEET_HEADER:
            if current:
                worksheet.delete_rows(1)
            worksheet.insert_row(SHEET_HEADER, index=1, value_input_option="USER_ENTERED")

    def append_postings(self, postings: Iterable[JobPosting]) -> int:
        """求人データを行として追記。重複 URL はスキップ。

        Returns:
            実際に追記した行数
        """
        worksheet = self._connect()
        existing_urls = set(worksheet.col_values(6)[1:])

        new_rows = [
            posting.to_row()
            for posting in postings
            if posting.job_url not in existing_urls
        ]

        if not new_rows:
            logger.info("追記対象の新規求人はありませんでした。")
            return 0

        worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        logger.info(f"{len(new_rows)} 件の求人をスプレッドシートに追記しました。")
        return len(new_rows)
