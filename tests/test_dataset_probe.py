"""Bright Data 等の既成データセットのサンプルを判定するツールのテスト。

購入前に「日本のデータが十分か」「電話番号が取れるか」を無料サンプルで測る。
"""

from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

import pytest

from src.dataset_probe import (
    ProbeReport,
    estimate,
    load_records,
    normalize_record,
    probe,
)


class TestNormalizeRecord:
    """ベンダーごとに異なるキー名を吸収して共通の形にする。"""

    def test_reads_bright_data_field_names(self) -> None:
        raw = {
            "company_name": "株式会社テスト",
            "description": "お問い合わせ 03-1234-5678",
            "location": "東京都 渋谷区",
            "country_code": "JP",
            "website": "https://example.co.jp",
            "url": "https://jp.indeed.com/viewjob?jk=abc",
        }

        record = normalize_record(raw)

        assert record.company_name == "株式会社テスト"
        assert record.description == "お問い合わせ 03-1234-5678"
        assert record.country_code == "JP"
        assert record.website == "https://example.co.jp"

    @pytest.mark.parametrize(
        "key", ["company_name", "company", "employer", "companyName"]
    )
    def test_accepts_company_name_aliases(self, key: str) -> None:
        assert normalize_record({key: "テスト社"}).company_name == "テスト社"

    @pytest.mark.parametrize(
        "key", ["description", "job_description", "descriptionText", "jobDescription"]
    )
    def test_accepts_description_aliases(self, key: str) -> None:
        assert normalize_record({key: "本文"}).description == "本文"

    @pytest.mark.parametrize(
        "key", ["website", "company_website", "companyWebsite", "company_link"]
    )
    def test_accepts_website_aliases(self, key: str) -> None:
        assert normalize_record({key: "https://a.jp"}).website == "https://a.jp"

    def test_missing_fields_become_none(self) -> None:
        record = normalize_record({})
        assert record.company_name is None
        assert record.description is None
        assert record.website is None

    def test_blank_strings_become_none(self) -> None:
        record = normalize_record({"company_name": "   ", "description": ""})
        assert record.company_name is None
        assert record.description is None


class TestIsJapan:
    """日本のレコードかを判定する (国コードが無いデータセットもあるため住所も見る)。"""

    @pytest.mark.parametrize("code", ["JP", "jp", "JPN", "Japan", "日本"])
    def test_country_code_variants_are_japan(self, code: str) -> None:
        assert normalize_record({"country_code": code}).is_japan is True

    def test_falls_back_to_location_when_country_code_missing(self) -> None:
        assert normalize_record({"location": "東京都 渋谷区"}).is_japan is True
        assert normalize_record({"location": "大阪府 大阪市"}).is_japan is True

    def test_non_japan_records_are_excluded(self) -> None:
        assert normalize_record({"country_code": "US"}).is_japan is False
        assert normalize_record({"location": "New York, NY"}).is_japan is False

    def test_country_code_wins_over_location(self) -> None:
        raw = {"country_code": "US", "location": "東京都"}
        assert normalize_record(raw).is_japan is False


def _jp(description: str | None = None, website: str | None = None,
        company: str = "テスト社") -> dict:
    return {
        "company_name": company,
        "description": description,
        "website": website,
        "country_code": "JP",
        "location": "東京都 渋谷区",
    }


class TestProbe:
    """購入判断に必要な数字を出す。"""

    def test_counts_japan_records_only(self) -> None:
        raws = [
            _jp("TEL 03-1234-5678"),
            {"company_name": "US Corp", "country_code": "US", "description": "call 555-1234"},
        ]

        report = probe(raws)

        assert report.total == 2
        assert report.japan == 1

    def test_counts_phone_extraction_from_description(self) -> None:
        raws = [
            _jp("お問い合わせ 03-1234-5678"),
            _jp("電話 090-1111-2222"),
            _jp("電話番号の記載はありません"),
        ]

        report = probe(raws)

        assert report.with_description == 3
        assert report.with_phone == 2

    def test_counts_unique_phones_for_dedupe_rate(self) -> None:
        raws = [
            _jp("TEL 03-1234-5678", company="A社"),
            _jp("TEL 03-1234-5678", company="B社"),  # 同じ番号 (チェーン本部等)
            _jp("TEL 06-9999-8888", company="C社"),
        ]

        report = probe(raws)

        assert report.with_phone == 3
        assert report.unique_phones == 2

    def test_counts_website_coverage_of_records_without_phone(self) -> None:
        """description に電話が無いレコードを企業サイト経由で救えるかを測る。"""
        raws = [
            _jp("TEL 03-1234-5678", website="https://has-phone.jp"),
            _jp("電話番号なし", website="https://no-phone.jp"),
            _jp("これも電話番号なし", website=None),
        ]

        report = probe(raws)

        assert report.without_phone == 2
        assert report.without_phone_with_website == 1

    def test_invalid_phone_numbers_are_not_counted(self) -> None:
        raws = [_jp("お電話 000-000-0000"), _jp("TEL 03-1234-5678")]

        report = probe(raws)

        assert report.with_phone == 1

    def test_rates_are_computed_against_japan_records(self) -> None:
        raws = [
            _jp("TEL 03-1234-5678"),
            _jp("電話なし"),
            {"company_name": "US", "country_code": "US", "description": "x"},
        ]

        report = probe(raws)

        assert report.japan_rate == pytest.approx(2 / 3)
        assert report.phone_rate == pytest.approx(1 / 2)

    def test_empty_input_does_not_divide_by_zero(self) -> None:
        report = probe([])

        assert report.total == 0
        assert report.japan_rate == 0.0
        assert report.phone_rate == 0.0


class TestLoadRecords:
    """ベンダーの配布形式 (JSON / NDJSON / CSV、いずれも .gz あり) を読む。"""

    RECORDS = [
        {"company_name": "A社", "description": "TEL 03-1234-5678", "country_code": "JP"},
        {"company_name": "B社", "description": "電話なし", "country_code": "JP"},
    ]

    def test_reads_json_array(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.json"
        path.write_text(json.dumps(self.RECORDS), encoding="utf-8")

        assert load_records(path) == self.RECORDS

    def test_reads_ndjson(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.ndjson"
        path.write_text(
            "\n".join(json.dumps(r) for r in self.RECORDS), encoding="utf-8"
        )

        assert load_records(path) == self.RECORDS

    def test_reads_jsonl_extension(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.jsonl"
        path.write_text(
            "\n".join(json.dumps(r) for r in self.RECORDS), encoding="utf-8"
        )

        assert load_records(path) == self.RECORDS

    def test_reads_csv(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(self.RECORDS[0]))
            writer.writeheader()
            writer.writerows(self.RECORDS)

        assert load_records(path) == self.RECORDS

    def test_reads_gzipped_ndjson(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.ndjson.gz"
        payload = "\n".join(json.dumps(r) for r in self.RECORDS)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(payload)

        assert load_records(path) == self.RECORDS

    def test_ignores_blank_lines_in_ndjson(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.ndjson"
        path.write_text(
            json.dumps(self.RECORDS[0]) + "\n\n" + json.dumps(self.RECORDS[1]) + "\n",
            encoding="utf-8",
        )

        assert load_records(path) == self.RECORDS

    def test_json_array_wrapped_in_object_is_unwrapped(self, tmp_path: Path) -> None:
        """{"data": [...]} 形式で配布されることがある。"""
        path = tmp_path / "sample.json"
        path.write_text(json.dumps({"data": self.RECORDS}), encoding="utf-8")

        assert load_records(path) == self.RECORDS

    def test_unknown_extension_raises_with_actionable_message(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "sample.parquet"
        path.write_bytes(b"PAR1")

        with pytest.raises(ValueError, match="parquet"):
            load_records(path)


class TestEstimate:
    """サンプルの実測値から、実際に買ったときの単価を逆算する。"""

    # 日本 1,000 件 / 電話あり 400 件 (ユニーク 300) / 電話なし 600 件のうち 300 件にサイトあり
    REPORT = ProbeReport(
        total=1000, japan=1000, with_description=1000,
        with_phone=400, unique_phones=300,
        without_phone=600, without_phone_with_website=300,
    )

    def test_description_only_unit_cost(self) -> None:
        """description からの抽出だけで納品する場合の単価。"""
        est = estimate(self.REPORT, record_price_usd=0.0025, jpy_rate=150.0,
                       contact_price_usd=None)

        # 1,000 件 x $0.0025 = $2.5 = 375円 で 300 件納品 → 1.25円/件
        assert est.delivered == 300
        assert est.total_cost_jpy == pytest.approx(375.0)
        assert est.unit_cost_jpy == pytest.approx(1.25)

    def test_adding_contact_scraper_uses_success_only_pricing(self) -> None:
        """企業サイト経由の補完は「見つかった分だけ」課金される。"""
        est = estimate(self.REPORT, record_price_usd=0.0025, jpy_rate=150.0,
                       contact_price_usd=0.0045, contact_success_rate=0.7)

        # サイトあり 300 件 x 成功率 0.7 = 210 件を追加取得、課金は成功分のみ
        # コスト = 375円 + 210 x $0.0045 x 150 = 375 + 141.75 = 516.75円
        # 納品 = 300 + 210 = 510 件
        assert est.delivered == 510
        assert est.total_cost_jpy == pytest.approx(516.75)
        assert est.unit_cost_jpy == pytest.approx(516.75 / 510)

    def test_projects_records_needed_for_a_target(self) -> None:
        """目標件数に必要な購入レコード数を出す。"""
        est = estimate(self.REPORT, record_price_usd=0.0025, jpy_rate=150.0,
                       contact_price_usd=None, target=41688)

        # 1,000 レコード買うと 300 件納品できる → 41,688 件には 138,960 レコード必要
        assert est.records_needed_for_target == pytest.approx(138960, rel=1e-3)
        assert est.target_cost_jpy == pytest.approx(41688 * 1.25)

    def test_zero_delivered_does_not_divide_by_zero(self) -> None:
        est = estimate(ProbeReport(total=10, japan=0), record_price_usd=0.0025,
                       jpy_rate=150.0, contact_price_usd=None)

        assert est.delivered == 0
        assert est.unit_cost_jpy == 0.0
