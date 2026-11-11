"""只使用合成记录验证规则，不访问网站、真实库或账号。"""

import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from product_review import evaluate_request, export_csv


def request():
    return {
        "ip_name": "测试IP",
        "platform": "test",
        "target_site": "https://example.com",
        "policy_confirmed": True,
        "policy": {
            "threshold_boundary": "inclusive",
            "corrected_filter": "keep_initial",
            "range_price": "review",
            "missing_data": "review",
            "ambiguous_counts": "review",
        },
        "whitelist": [],
        "products": [
            {
                "title": "测试商品",
                "url": "https://example.com/item/1",
                "shop_id": "001",
                "shop_name": "同名店",
                "shop_url": "https://example.com/shop/001",
                "price_raw": "￥500元",
                "sales_raw": "1000",
                "comments_raw": "80",
                "crawled_at": "2026-11-08T10:00:00+08:00",
                "detail_crawled_at": "2026-11-08T10:01:00+08:00",
            }
        ],
    }


class ProductReviewTests(unittest.TestCase):
    def test_formula_preserves_raw_and_blank_review(self):
        data = request()
        original = copy.deepcopy(data)
        row = evaluate_request(data)[0]
        self.assertEqual(row["initial_amount"], 500000)
        self.assertEqual(row["corrected_sales"], 800)
        self.assertEqual(row["corrected_amount"], 400000)
        self.assertEqual(row["status"], "人工审核候选")
        self.assertEqual(row["price_raw"], "￥500元")
        self.assertEqual(row["review_result"], "")
        self.assertEqual(row["notes"], "")
        self.assertEqual(data, original)

    def test_boundaries_and_detail_gate(self):
        for boundary, initial, needs_detail in (
            ("inclusive", "500000", True),
            ("exclusive", "500000", False),
        ):
            with self.subTest(boundary=boundary):
                data = request()
                data["policy"]["threshold_boundary"] = boundary
                data["products"][0]["comments_raw"] = ""
                row = evaluate_request(data)[0]
                self.assertEqual(str(row["initial_amount"]), initial + ".00")
                self.assertEqual(row["needs_detail"], needs_detail)
                self.assertIsNone(row["corrected_amount"])

    def test_corrected_filter_must_use_selected_rule(self):
        data = request()
        data["policy"]["corrected_filter"] = "require_threshold"
        self.assertEqual(evaluate_request(data)[0]["status"], "修正金额未达门槛")
        data["products"][0]["comments_raw"] = "5000"
        row = evaluate_request(data)[0]
        self.assertEqual(row["corrected_sales"], 1000)
        self.assertEqual(row["status"], "人工审核候选")

    def test_corrected_threshold_equality_obeys_boundary(self):
        data = request()
        data["policy"]["corrected_filter"] = "require_threshold"
        data["products"][0].update(sales_raw="2000", comments_raw="100")
        self.assertEqual(evaluate_request(data)[0]["status"], "人工审核候选")
        data["policy"]["threshold_boundary"] = "exclusive"
        row = evaluate_request(data)[0]
        self.assertEqual(row["initial_amount"], 1000000)
        self.assertEqual(row["corrected_amount"], 500000)
        self.assertEqual(row["status"], "修正金额未达门槛")

    def test_same_shop_products_do_not_aggregate_to_threshold(self):
        data = request()
        data["products"][0].update(price_raw="300", comments_raw="")
        data["products"].append(copy.deepcopy(data["products"][0]))
        data["products"][1]["url"] = "https://example.com/item/2"
        rows = evaluate_request(data)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "初始金额未达门槛" for row in rows))
        self.assertTrue(all(not row["needs_detail"] for row in rows))

    def test_decimal_money_and_zero_sales(self):
        data = request()
        data["products"][0].update(
            price_raw="500.01", sales_raw="1000", comments_raw="80"
        )
        row = evaluate_request(data)[0]
        self.assertEqual(str(row["initial_amount"]), "500010.00")
        self.assertEqual(str(row["corrected_amount"]), "400008.00")
        data["products"][0]["sales_raw"] = "0"
        row = evaluate_request(data)[0]
        self.assertEqual(row["sales"], 0)
        self.assertEqual(row["initial_amount"], 0)
        self.assertEqual(row["status"], "初始金额未达门槛")

    def test_missing_values_are_never_zero_or_fallback(self):
        for field in ("price_raw", "sales_raw", "comments_raw"):
            with self.subTest(field=field):
                data = request()
                data["products"][0][field] = ""
                row = evaluate_request(data)[0]
                self.assertIsNone(row["corrected_sales"])
                self.assertIsNone(row["corrected_amount"])
                self.assertEqual(row["status"], "待人工核查")
                self.assertTrue(row["anomalies"])
                data["policy"]["missing_data"] = "exclude"
                self.assertEqual(evaluate_request(data)[0]["status"], "异常排除")

    def test_zero_comments_is_real_zero(self):
        data = request()
        data["products"][0]["comments_raw"] = "0"
        row = evaluate_request(data)[0]
        self.assertEqual(row["corrected_amount"], 0)
        self.assertFalse(row["needs_detail"])

    def test_range_prices_and_multispec(self):
        data = request()
        data["products"][0]["price_raw"] = "￥500-600元"
        self.assertIsNone(evaluate_request(data)[0]["price"])
        for mode, amount in (("minimum", 500000), ("maximum", 600000)):
            data["policy"]["range_price"] = mode
            self.assertEqual(evaluate_request(data)[0]["initial_amount"], amount)
        for raw in ("规格A 500元 规格B 600元", "券后500", "USD500", "NaN", "-5", "0"):
            data["products"][0]["price_raw"] = raw
            self.assertIsNone(evaluate_request(data)[0]["price"])

    def test_ambiguous_counts_require_explicit_policy(self):
        data = request()
        data["products"][0].update(sales_raw="已售1万+件", comments_raw="1000+条评价")
        self.assertIsNone(evaluate_request(data)[0]["sales"])
        data["policy"]["ambiguous_counts"] = "lower_bound"
        row = evaluate_request(data)[0]
        self.assertEqual(row["sales"], 10000)
        self.assertEqual(row["comments"], 1000)
        self.assertIn("下限", row["anomalies"])

    def test_qa_payment_people_never_imply_sold_units(self):
        for policy in ("review", "lower_bound"):
            for raw in ("1000人付款", "1000人已付款", "1万+人付款", "1000人收货"):
                with self.subTest(policy=policy, raw=raw):
                    data = request()
                    data["policy"]["ambiguous_counts"] = policy
                    data["products"][0]["sales_raw"] = raw
                    row = evaluate_request(data)[0]
                    self.assertEqual(row["sales_raw"], raw)
                    self.assertIsNone(row["sales"])
                    self.assertIsNone(row["initial_amount"])
                    self.assertIsNone(row["corrected_amount"])
                    self.assertEqual(row["status"], "待人工核查")
                    self.assertIn("指标", row["anomalies"])

    def test_qa_count_metrics_cannot_be_interchanged(self):
        for field, raw in (
            ("sales_raw", "评论1000条"),
            ("sales_raw", "1000人评价"),
            ("sales_raw", "月销1000"),
            ("comments_raw", "已售1000件"),
            ("comments_raw", "1000人付款"),
        ):
            with self.subTest(field=field, raw=raw):
                data = request()
                data["products"][0][field] = raw
                row = evaluate_request(data)[0]
                self.assertIsNone(row["corrected_amount"])
                self.assertEqual(row["status"], "待人工核查")
                self.assertTrue(row["anomalies"])

    def test_qa_invalid_detail_time_cannot_supply_correction(self):
        for mode in ("review", "exclude"):
            for detail_time in (
                "2020-01-01T00:00:00+08:00",
                "2026-11-08T01:59:59Z",
                "2026-11-08T10:01:00",
                "",
                "invalid",
            ):
                with self.subTest(mode=mode, detail_time=detail_time):
                    data = request()
                    data["policy"]["missing_data"] = mode
                    data["products"][0]["detail_crawled_at"] = detail_time
                    row = evaluate_request(data)[0]
                    self.assertEqual(row["initial_amount"], 500000)
                    self.assertEqual(row["comments"], 80)
                    self.assertEqual(row["detail_crawled_at"], detail_time)
                    self.assertIsNone(row["corrected_sales"])
                    self.assertIsNone(row["corrected_amount"])
                    self.assertTrue(row["needs_detail"])
                    self.assertEqual(
                        row["status"], "待人工核查" if mode == "review" else "异常排除"
                    )
                    self.assertTrue(row["anomalies"])

    def test_qa_equal_and_later_instants_across_timezones(self):
        for detail_time in (
            "2026-11-08T10:00:00+08:00",
            "2026-11-08T02:00:00Z",
            "2026-11-07T21:00:00-05:00",
            "2026-11-08T02:01:00Z",
        ):
            with self.subTest(detail_time=detail_time):
                data = request()
                data["products"][0]["detail_crawled_at"] = detail_time
                row = evaluate_request(data)[0]
                self.assertEqual(row["corrected_amount"], 400000)
                self.assertEqual(row["status"], "人工审核候选")
                self.assertEqual(row["anomalies"], "")

    def test_qa_original_failure_fixture_export(self):
        root = Path(__file__).resolve().parents[2]
        source = root / "doc" / "qa_product_review_20261108_input.json"
        script = Path(__file__).with_name("product_review.py")
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "recheck.csv"
            run = subprocess.run(
                [sys.executable, str(script), str(source), str(output)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["candidates"], 1)
            rows = list(
                csv.DictReader(io.StringIO(output.read_text(encoding="utf-8-sig")))
            )
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["修正销售额"], "400000.00")
            self.assertEqual(rows[1]["网站展示销量原文"], "1000人付款")
            self.assertEqual(rows[1]["初始销售额"], "")
            self.assertEqual(rows[2]["修正销售额"], "")
            for row in rows[1:]:
                self.assertEqual(row["处理状态"], "待人工核查")
                self.assertTrue(row["数据异常说明"])
            self.assertTrue(
                all(row["人工审核结论"] == row["备注"] == "" for row in rows)
            )

    def test_whitelist_scoping_and_same_name(self):
        data = request()
        entry = {
            "ip_name": "测试IP",
            "platform": "test",
            "shop_id": "002",
            "shop_name": "同名店",
            "shop_url": "https://example.com/shop/001",
        }
        data["whitelist"] = [entry]
        self.assertEqual(evaluate_request(data)[0]["status"], "人工审核候选")
        entry["shop_id"] = "001"
        self.assertEqual(evaluate_request(data)[0]["status"], "白名单排除")
        entry["ip_name"] = "其他IP"
        self.assertEqual(evaluate_request(data)[0]["status"], "人工审核候选")
        entry.update(ip_name="测试IP", platform="other")
        self.assertEqual(evaluate_request(data)[0]["status"], "人工审核候选")

    def test_url_whitelist_preserves_query_ids(self):
        data = request()
        data["products"][0].update(
            shop_id="", shop_url="https://example.com/shop?id=1&b=2"
        )
        data["whitelist"] = [
            {
                "platform": "test",
                "ip_name": "测试IP",
                "shop_url": "https://EXAMPLE.com/shop/?b=2&id=1#top",
            }
        ]
        self.assertEqual(evaluate_request(data)[0]["status"], "白名单排除")
        data["whitelist"][0]["shop_url"] = "https://example.com/shop?b=2&id=2"
        self.assertEqual(evaluate_request(data)[0]["status"], "人工审核候选")

    def test_unconfirmed_or_incomplete_request_rejected(self):
        for key in (
            "policy_confirmed",
            "target_site",
            "ip_name",
            "platform",
            "whitelist",
            "policy",
        ):
            data = request()
            del data[key]
            with self.subTest(key=key), self.assertRaises((ValueError, KeyError)):
                evaluate_request(data)
        data = request()
        del data["policy"]["threshold_boundary"]
        with self.assertRaises(TypeError):
            evaluate_request(data)
        data = request()
        data["whitelist"] = [
            {"platform": "test", "ip_name": "测试IP", "shop_name": "同名店"}
        ]
        with self.assertRaises(ValueError):
            evaluate_request(data)

    def test_every_policy_field_required_and_rejects_unknown_choices(self):
        for key in request()["policy"]:
            data = request()
            del data["policy"][key]
            with self.subTest(missing=key), self.assertRaises(TypeError):
                evaluate_request(data)
            data = request()
            data["policy"][key] = "automatic"
            with self.subTest(invalid=key), self.assertRaises(ValueError):
                evaluate_request(data)

    def test_count_parser_does_not_extract_unrelated_or_fractional_counts(self):
        for raw in ("1000人看过", "约1000", "1000-2000", "1.5", "-1", "NaN"):
            data = request()
            data["products"][0]["sales_raw"] = raw
            with self.subTest(raw=raw):
                row = evaluate_request(data)[0]
                self.assertIsNone(row["sales"])
                self.assertIsNone(row["initial_amount"])
                self.assertEqual(row["status"], "待人工核查")

    def test_missing_identity_and_capture_time_cannot_be_candidate(self):
        for changes in (
            {"shop_id": "", "shop_url": ""},
            {"url": "javascript:alert(1)"},
            {"crawled_at": ""},
            {"detail_crawled_at": "2026-11-08"},
        ):
            data = request()
            data["products"][0].update(changes)
            with self.subTest(changes=changes):
                row = evaluate_request(data)[0]
                self.assertEqual(row["status"], "待人工核查")
                self.assertTrue(row["anomalies"])

    def test_csv_retains_audit_rows_and_protects_formula_cells(self):
        data = request()
        data["products"][0]["title"] = '=HYPERLINK("https://example.com")'
        rows = evaluate_request(data)
        parsed = list(csv.DictReader(io.StringIO(export_csv(rows))))
        self.assertTrue(parsed[0]["商品名称"].startswith("'="))
        self.assertEqual(parsed[0]["初始销售额"], "500000.00")
        self.assertEqual(parsed[0]["人工审核结论"], "")
        self.assertEqual(parsed[0]["备注"], "")
        self.assertEqual(parsed[0]["商品单价原文"], "￥500元")

    def test_cli_end_to_end_and_no_overwrite(self):
        script = Path(__file__).with_name("product_review.py")
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "input.json", Path(folder) / "output.csv"
            source.write_text(
                json.dumps(request(), ensure_ascii=False), encoding="utf-8"
            )
            run = subprocess.run(
                [sys.executable, str(script), str(source), str(output)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["candidates"], 1)
            self.assertTrue(output.read_bytes().startswith(b"\xef\xbb\xbf"))
            parsed = list(
                csv.DictReader(io.StringIO(output.read_text(encoding="utf-8-sig")))
            )
            self.assertEqual(parsed[0]["修正销售额"], "400000.00")
            second = subprocess.run(
                [sys.executable, str(script), str(source), str(output)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(second.returncode, 2)


if __name__ == "__main__":
    unittest.main()
