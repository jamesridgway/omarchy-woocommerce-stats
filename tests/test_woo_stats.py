"""Run with: python3 -m unittest discover tests"""

import datetime as dt
import http.server
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import woo_stats as ws

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 15, 11, 0, tzinfo=UTC)
KEY, SECRET = "ck_testkey", "cs_testsecret"


def options(**overrides):
    base = {
        "statuses": "processing,completed", "assumedMargin": 0.0, "costMetaKeys": [],
        "barFormat": "{revenue}|{orders}|{profit}|{margin}", "barPeriod": "today",
        "maxAge": 270, "historyMinutes": 60,
    }
    base.update(overrides)
    return base


def order(total="120.00", tax="20.00", lines=None, date="2026-08-15T09:00:00", **extra):
    data = {"id": 1, "status": "processing", "total": total, "total_tax": tax, "currency": "GBP",
            "date_created_gmt": date, "refunds": [], "meta_data": [],
            "line_items": lines if lines is not None else [line()]}
    data.update(extra)
    return data


def line(total="100.00", cost=None, quantity=1, product=10, variation=0, meta=None):
    data = {"total": total, "quantity": quantity, "product_id": product, "variation_id": variation,
            "meta_data": meta or []}
    if cost is not None:
        data["cost_of_goods_sold"] = {"value": cost}
    return data


class OrderMaths(unittest.TestCase):
    def summarize(self, data, opts=None, costs=None):
        return ws.summarize_order(data, opts or options(), costs or {})

    def test_revenue_excludes_tax_and_gross_includes_it(self):
        result = self.summarize(order())
        self.assertEqual(result["gross"], 120)
        self.assertEqual(result["net"], 100)

    def test_partial_refund_scales_revenue_and_cost(self):
        data = order(lines=[line(cost=40)], refunds=[{"id": 9, "total": "-60.00"}])
        result = self.summarize(data)
        self.assertEqual(result["gross"], 60)
        self.assertEqual(result["net"], 50)
        self.assertEqual(result["cost"], 20)
        self.assertEqual(result["costedNet"], 50)

    def test_refund_larger_than_total_does_not_go_negative(self):
        result = self.summarize(order(refunds=[{"total": "-500"}]))
        self.assertEqual(result["gross"], 0)
        self.assertEqual(result["net"], 0)

    def test_core_cost_is_line_total_not_multiplied(self):
        result = self.summarize(order(lines=[line(cost=40, quantity=2)]))
        self.assertEqual(result["cost"], 40)

    def test_unit_meta_cost_is_multiplied_by_quantity(self):
        meta = [{"key": "_alg_wc_cog_item_cost", "value": "15"}]
        result = self.summarize(order(lines=[line(quantity=3, meta=meta)]))
        self.assertEqual(result["cost"], 45)

    def test_custom_meta_key_is_used(self):
        meta = [{"key": "_my_cost", "value": "7"}]
        result = self.summarize(order(lines=[line(meta=meta)]), options(costMetaKeys=["_my_cost"]))
        self.assertEqual(result["cost"], 7)

    def test_product_cost_is_fallback(self):
        result = self.summarize(order(lines=[line(quantity=2)]), costs={"10:0": {"cost": 30}})
        self.assertEqual(result["cost"], 60)

    def test_order_level_cost_used_only_when_lines_have_none(self):
        result = self.summarize(order(cost_of_goods_sold={"total_value": 55}))
        self.assertEqual((result["cost"], result["costedNet"]), (55, 100))
        both = order(lines=[line(cost=40)], cost_of_goods_sold={"total_value": 55})
        self.assertEqual(self.summarize(both)["cost"], 40)

    def test_order_level_cost_beats_cached_product_cost(self):
        data = order(cost_of_goods_sold={"total_value": 55})
        self.assertEqual(self.summarize(data, costs={"10:0": {"cost": 30}})["cost"], 55)

    def test_assumed_margin_only_applies_to_uncosted_lines(self):
        data = order(lines=[line("100", cost=40), line("100", product=11)])
        result = self.summarize(data, options(assumedMargin=25))
        self.assertEqual(result["costedNet"], 100)
        self.assertEqual(result["assumedNet"], 100)
        self.assertEqual(result["cost"], 40 + 75)

    def test_malformed_numbers_count_as_zero(self):
        result = self.summarize(order(total="n/a", tax=None, lines=[line(total="nan", cost="x")]))
        self.assertEqual(result["net"], 0)
        self.assertEqual(result["cost"], 0)


class Periods(unittest.TestCase):
    def metrics(self, orders, opts=None, costs=None, tz=UTC, when=dt.date(2026, 8, 15)):
        days = ws.aggregate(orders, tz, "GBP", opts or options(), costs or {})
        return ws.period_metrics(days, [when])

    def test_no_cost_data_means_unknown_profit_not_zero(self):
        result = self.metrics([order()])
        self.assertIsNone(result["profit"])
        self.assertIsNone(result["margin"])

    def test_no_orders_is_zero_profit_and_unknown_margin(self):
        result = self.metrics([])
        self.assertEqual(result["profit"], 0)
        self.assertIsNone(result["margin"])

    def test_partial_coverage_reports_only_known_sales(self):
        result = self.metrics([order(total="200", tax="0", lines=[line("100", cost=60), line("100", product=11)])])
        self.assertEqual(result["profit"], 40)
        self.assertEqual(result["margin"], 40)
        self.assertEqual(result["coverage"], 0.5)
        self.assertTrue(ws.is_estimate(result))

    def test_day_buckets_use_store_timezone(self):
        london = ws.ZoneInfo("Europe/London")
        late = order(date="2026-08-14T23:30:00")  # 00:30 BST on the 15th
        self.assertEqual(self.metrics([late], tz=london)["orders"], 1)
        self.assertEqual(self.metrics([late], tz=UTC)["orders"], 0)

    def test_other_currency_is_excluded_and_reported(self):
        result = self.metrics([order(), order(currency="EUR")])
        self.assertEqual(result["orders"], 1)
        self.assertEqual(result["otherCurrency"], 1)
        self.assertIn("another currency", ws.coverage_note(result))

    def test_period_ranges(self):
        today = dt.date(2026, 8, 15)
        self.assertEqual(len(ws.period_days("7d", today)), 7)
        self.assertEqual(len(ws.period_days("mtd", today)), 15)
        self.assertEqual(ws.period_days("yesterday", dt.date(2026, 8, 1)), [dt.date(2026, 7, 31)])

    def test_every_period_range(self):
        today = dt.date(2026, 8, 12)  # a Wednesday
        d = dt.date
        expected = {
            "today": (today, today),
            "yesterday": (d(2026, 8, 11), d(2026, 8, 11)),
            "wtd": (d(2026, 8, 10), today),
            "last_week": (d(2026, 8, 3), d(2026, 8, 9)),
            "7d": (d(2026, 8, 6), today),
            "30d": (d(2026, 7, 14), today),
            "90d": (d(2026, 5, 15), today),
            "mtd": (d(2026, 8, 1), today),
            "last_month": (d(2026, 7, 1), d(2026, 7, 31)),
            "qtd": (d(2026, 7, 1), today),
            "last_quarter": (d(2026, 4, 1), d(2026, 6, 30)),
            "ytd": (d(2026, 1, 1), today),
            "last_year": (d(2025, 1, 1), d(2025, 12, 31)),
        }
        self.assertEqual(set(expected), set(ws.PERIOD_LABELS))
        for key, value in expected.items():
            self.assertEqual(ws.period_range(key, today), value, key)

    def test_ranges_across_year_boundary(self):
        today = dt.date(2026, 1, 15)
        self.assertEqual(ws.period_range("last_month", today), (dt.date(2025, 12, 1), dt.date(2025, 12, 31)))
        self.assertEqual(ws.period_range("last_quarter", today), (dt.date(2025, 10, 1), dt.date(2025, 12, 31)))

    def test_week_can_start_on_sunday(self):
        today = dt.date(2026, 8, 12)
        self.assertEqual(ws.period_range("wtd", today, 6)[0], dt.date(2026, 8, 9))
        self.assertEqual(ws.period_range("wtd", dt.date(2026, 8, 9), 6)[0], dt.date(2026, 8, 9))
        self.assertEqual(ws.period_range("last_week", today, 6), (dt.date(2026, 8, 2), dt.date(2026, 8, 8)))

    def test_needed_start_covers_periods_bar_and_trend(self):
        today = dt.date(2026, 8, 12)
        base = options(periods=["today"], trendDays=0, weekStart="monday")
        self.assertEqual(ws.needed_start(base, today), today)
        self.assertEqual(ws.needed_start({**base, "trendDays": 7}, today), dt.date(2026, 8, 6))
        self.assertEqual(ws.needed_start({**base, "barPeriod": "mtd"}, today), dt.date(2026, 8, 1))
        self.assertEqual(ws.needed_start({**base, "periods": ["last_year"]}, today), dt.date(2025, 1, 1))

    def test_unloaded_period_is_unknown_rather_than_a_low_total(self):
        days = {"2026-08-12": {**ws.empty_day(), "orders": 1, "net": 100.0}}
        result = ws.period_metrics(days, [dt.date(2026, 8, 1), dt.date(2026, 8, 12)], "2026-08-05")
        self.assertTrue(result["loading"])
        self.assertIsNone(result["revenue"])


class Display(unittest.TestCase):
    def test_money(self):
        self.assertEqual(ws.money(1234.5, "£"), "£1,234.50")
        self.assertEqual(ws.money(1234.5, "£", compact=True), "£1,234")
        self.assertEqual(ws.money(-5, "£", compact=True), "-£5")
        self.assertEqual(ws.money(250000, "£", compact=True), "£250k")
        self.assertEqual(ws.money(None, "£"), "—")

    def test_render_leaves_unknown_fields_visible(self):
        self.assertEqual(ws.render_bar("{orders} {nope}", {"orders": "3"}), "3 {nope}")


class CurrencyFormats(unittest.TestCase):
    def fmt(self, **overrides):
        return {"symbol": "€", "position": "right_space", "thousand": ".", "decimal": ",", "decimals": 2, **overrides}

    def test_european_format(self):
        self.assertEqual(ws.money(1234.5, self.fmt()), "1.234,50 €")
        self.assertEqual(ws.money(1234.5, self.fmt(), compact=True), "1.234 €")
        self.assertEqual(ws.money(-5, self.fmt(), compact=True), "-5 €")

    def test_every_symbol_position(self):
        for position, expected in (("left", "$5.00"), ("right", "5.00$"), ("left_space", "$ 5.00"), ("right_space", "5.00 $")):
            fmt = self.fmt(symbol="$", position=position, thousand=",", decimal=".")
            self.assertEqual(ws.money(5, fmt), expected)

    def test_zero_decimal_currency_has_no_trailing_separator(self):
        self.assertEqual(ws.money(1234, self.fmt(symbol="¥", position="left", decimals=0, thousand=",")), "¥1,234")

    def test_empty_thousand_separator(self):
        self.assertEqual(ws.money(1234.5, self.fmt(thousand="")), "1234,50 €")

    def test_compact_thousands_use_store_separators(self):
        self.assertEqual(ws.money(1250000, self.fmt(), compact=True), "1.250k €")

    def test_missing_symbol_falls_back_to_currency_code(self):
        self.assertEqual(ws.money(5, ws.store_format({"currency": "CHF", "symbol": ""})), "CHF 5.00")


class CostReport(unittest.TestCase):
    def report(self, orders, opts=None, costs=None):
        return ws.cost_report(orders, opts or options(), costs or {})

    def test_verdicts(self):
        self.assertEqual(self.report([])["verdict"], "no_orders")
        self.assertEqual(self.report([order()])["verdict"], "none")
        self.assertEqual(self.report([order(lines=[line(cost=5), line()])])["verdict"], "partial")
        self.assertEqual(self.report([order(lines=[line(cost=5)])])["verdict"], "full")

    def test_order_level_cost_counts_as_covered(self):
        result = self.report([order(cost_of_goods_sold={"total_value": 9})])
        self.assertEqual(result["verdict"], "full")

    def test_suggests_likely_keys_without_their_values(self):
        meta = [{"key": "_supplier_price", "value": "12.34"}, {"key": "colour", "value": "red"}]
        result = self.report([order(lines=[line(meta=meta)])])
        self.assertEqual(result["candidates"], ["_supplier_price"])
        self.assertNotIn("12.34", json.dumps(result))

    def test_configured_key_is_not_suggested(self):
        meta = [{"key": "_supplier_price", "value": "12"}]
        result = self.report([order(lines=[line(meta=meta)])], options(costMetaKeys=["_supplier_price"]))
        self.assertEqual((result["verdict"], result["candidates"]), ("full", []))

    def test_report_text_matches_the_verdict(self):
        cases = (([], "can't be checked"), ([order()], "not available yet"),
                 ([order(lines=[line(cost=5), line()])], "50% of these lines"), ([order(lines=[line(cost=5)])], "ready"))
        for orders, expected in cases:
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                ws.print_cost_report(self.report(orders))
            self.assertIn(expected, out.getvalue())


class Trend(unittest.TestCase):
    def test_nice_ceiling(self):
        for value, expected in ((0.4, 1), (1, 1), (3, 3), (7, 8), (120, 200), (500, 500), (501, 600), (310, 400)):
            self.assertEqual(ws.nice_ceiling(value), expected)

    def test_axis_handles_losses_and_empty_data(self):
        money = lambda value: f"£{value:,.0f}"
        self.assertEqual(ws.trend_axis([120, -30], money)["top"], 200)
        self.assertEqual(ws.trend_axis([120, -35], money)["bottom"], -40)
        self.assertEqual(ws.trend_axis([-30], money)["top"], 0)
        empty = ws.trend_axis([0, None], money)
        self.assertEqual((empty["top"], empty["bottom"], empty["topText"]), (1, 0, ""))

    def build(self, days, length=3):
        return ws.build_trend(days, dt.date(2026, 8, 15), options(trendDays=length), "£", None)

    def test_quiet_days_are_zero_and_oldest_comes_first(self):
        trend = self.build({})
        self.assertEqual([row["date"] for row in trend["days"]], ["2026-08-13", "2026-08-14", "2026-08-15"])
        self.assertEqual(trend["days"][0]["revenue"], 0)
        self.assertTrue(trend["days"][-1]["today"])
        self.assertEqual(trend["days"][-1]["label"], "Today so far")

    def test_unknown_profit_stays_a_gap_not_zero(self):
        day = {**ws.empty_day(), "orders": 1, "net": 50.0, "productNet": 50.0}
        trend = self.build({"2026-08-15": day})
        self.assertIsNone(trend["days"][-1]["profit"])
        self.assertIn("— profit", trend["days"][-1]["detail"])

    def test_totals_match_sum_of_days(self):
        day = {**ws.empty_day(), "orders": 2, "net": 50.0, "productNet": 50.0, "costedNet": 50.0, "cost": 20.0}
        trend = self.build({"2026-08-14": day, "2026-08-15": day})
        self.assertEqual(trend["totals"], {"revenue": "£100.00", "profit": "£60.00", "orders": "4"})
        self.assertIn("2 orders", trend["days"][-1]["detail"])

    def test_trend_can_be_turned_off(self):
        self.assertIsNone(self.build({}, length=0))


class Prefs(unittest.TestCase):
    def test_unknown_values_fall_back_and_order_is_fixed(self):
        prefs = ws.normalize_prefs({"periods": ["ytd", "bogus", "today"], "trendDays": 11, "trendMetric": "x", "weekStart": 3})
        self.assertEqual(prefs["periods"], ["today", "ytd"])
        self.assertEqual(prefs["trendDays"], 30)
        self.assertEqual(prefs["trendMetric"], "revenue")
        self.assertEqual(prefs["weekStart"], "monday")

    def test_empty_selection_uses_defaults(self):
        self.assertEqual(ws.normalize_prefs({"periods": []})["periods"], ws.DEFAULT_PREFS["periods"])
        self.assertEqual(ws.normalize_prefs("nonsense")["trendDays"], 30)

    def test_trend_days_zero_is_kept(self):
        self.assertEqual(ws.normalize_prefs({"trendDays": 0})["trendDays"], 0)


class MockStore:
    """A local WooCommerce REST API that records every request."""

    def __init__(self):
        self.orders = [order(lines=[line(cost=40)])]
        self.requests = []
        self.fail_with = None
        self.strip_basic_auth = False
        self.redirect_to = None
        self.rest_prefix = "/wp-json"
        self.plain_permalinks = False
        self.plain_rejects_keys = False
        self.block_public_root = False
        self.deny_settings = False
        self.link_override = None
        self.home_requests = []
        self.settings = [
            {"id": "woocommerce_currency_pos", "value": "left"},
            {"id": "woocommerce_price_thousand_sep", "value": ","},
            {"id": "woocommerce_price_decimal_sep", "value": "."},
            {"id": "woocommerce_price_num_decimals", "value": "2"},
        ]
        store = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query))
                store.requests.append((parts.path, query, dict(self.headers)))
                if store.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", store.redirect_to)
                    self.end_headers()
                    return
                if store.fail_with:
                    self.reply({"code": "fail"}, status=store.fail_with)
                    return
                route = self.route(parts.path, query)
                if route is None:
                    if parts.path == "/":
                        store.home_requests.append(dict(self.headers))
                        self.reply_home()
                    else:
                        self.reply({"code": "rest_no_route"}, status=404)
                    return
                if route == "/":
                    if store.block_public_root:
                        self.reply({"code": "rest_login_required"}, status=401)
                    else:
                        self.reply({"name": "Test &amp; Shop", "timezone_string": "Europe/London", "gmt_offset": 1})
                    return
                if store.plain_permalinks and store.plain_rejects_keys:
                    self.reply({"code": "woocommerce_rest_cannot_view"}, status=401)
                    return
                if not self.authorized(query):
                    self.reply({"code": "woocommerce_rest_cannot_view"}, status=401)
                    return
                if route.endswith("/settings/general"):
                    if store.deny_settings:
                        self.reply({"code": "woocommerce_rest_cannot_view"}, status=403)
                    else:
                        self.reply(store.settings)
                elif route.endswith("/data/currencies/current"):
                    self.reply({"code": "GBP", "symbol": "&pound;"})
                elif route.endswith("/orders"):
                    self.reply_orders(query)
                elif route.endswith("/products"):
                    self.reply([{"id": 10, "cost_of_goods_sold": {"total_value": 25}}])
                elif "/variations" in route:
                    self.reply([{"id": 77, "meta_data": [{"key": "_wc_cog_cost", "value": "12"}]}])
                else:
                    self.reply({}, status=404)

            def route(self, path, query):
                """The REST route this URL reaches, or None if it isn't the API."""
                if store.plain_permalinks:
                    return query.get("rest_route")
                prefix = store.rest_prefix + "/"
                return "/" + path[len(prefix):] if path.startswith(prefix) else None

            def reply_home(self):
                root = store.link_override
                if root is None:
                    root = f"{store.url}/?rest_route=/" if store.plain_permalinks else f"{store.url}{store.rest_prefix}/"
                raw = b"<html>Home</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                if root:
                    self.send_header("Link", f'<{root}>; rel="https://api.w.org/"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self, query):
                if store.plain_permalinks and "Authorization" in self.headers and not store.strip_basic_auth:
                    return True
                if query.get("consumer_key") == KEY and query.get("consumer_secret") == SECRET:
                    return True
                if store.strip_basic_auth:
                    return False
                return "Authorization" in self.headers

            def reply_orders(self, query):
                after = query.get("after", "")
                matching = [o for o in store.orders if o["date_created_gmt"] > after]
                if query.get("order") == "desc":
                    matching = list(reversed(matching))
                per_page = int(query.get("per_page", 10))
                page = int(query.get("page", 1))
                pages = max(1, (len(matching) + per_page - 1) // per_page)
                start = (page - 1) * per_page
                self.reply(matching[start:start + per_page], headers={"X-WP-TotalPages": str(pages)})

            def reply(self, body, status=200, headers=None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self):
        return [path for path, _, _ in self.requests]


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patch = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.temp.name, "config"),
            "XDG_CACHE_HOME": os.path.join(self.temp.name, "cache"),
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.store = MockStore()
        self.addCleanup(self.store.close)
        ws.write_private_json(ws.config_path("default"), {
            "url": self.store.url, "credentialStore": "file", "consumerKey": KEY, "consumerSecret": SECRET,
        })


class Status(StoreTestCase):
    def test_full_snapshot(self):
        result = ws.run_status("default", options(), now=NOW)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["store"]["name"], "Test & Shop")
        self.assertEqual(result["barText"], "£100|1|£60|60%")
        today = result["periods"][0]
        self.assertEqual((today["revenue"], today["profit"], today["margin"]), ("£100.00", "£60.00", "60%"))

    def test_missing_config_prompts_setup_without_network(self):
        os.unlink(ws.config_path("default"))
        result = ws.run_status("default", options(), now=NOW)
        self.assertEqual(result["state"], "not_configured")
        self.assertEqual(self.store.requests, [])

    def test_second_poll_uses_cache(self):
        ws.run_status("default", options(), now=NOW)
        count = len(self.store.requests)
        ws.run_status("default", options(), now=NOW + dt.timedelta(seconds=60))
        self.assertEqual(len(self.store.requests), count)

    def test_routine_poll_fetches_today_only_and_keeps_history(self):
        self.store.orders.append(order(date="2026-08-14T09:00:00", lines=[line(cost=10)]))
        ws.run_status("default", options(), now=NOW)
        self.store.requests.clear()
        self.store.orders.append(order(date="2026-08-15T11:30:00", lines=[line(cost=10)]))
        result = ws.run_status("default", options(), now=NOW + dt.timedelta(minutes=31))
        self.assertEqual([p for p in self.store.paths() if p.endswith("/wp-json/")], [])
        after = [q["after"] for p, q, _ in self.store.requests if p.endswith("/orders")]
        self.assertEqual(after, ["2026-08-14T22:59:59"])  # midnight BST, minus one second
        byKey = {p["key"]: p for p in result["periods"]}
        self.assertEqual(byKey["today"]["orders"], "2")
        self.assertEqual(byKey["yesterday"]["orders"], "1")

    def test_history_refreshes_after_interval_and_at_midnight(self):
        ws.run_status("default", options(), now=NOW)
        self.store.requests.clear()
        ws.run_status("default", options(), now=NOW + dt.timedelta(minutes=61))
        self.assertIn("/wp-json/", self.store.paths())
        self.store.requests.clear()
        # 23:30 UTC is 00:30 the next day in London
        ws.run_status("default", options(), now=dt.datetime(2026, 8, 15, 23, 30, tzinfo=UTC) - dt.timedelta(minutes=45))
        self.store.requests.clear()
        ws.run_status("default", options(), now=dt.datetime(2026, 8, 15, 23, 30, tzinfo=UTC))
        self.assertIn("/wp-json/", self.store.paths())

    def test_changing_settings_refetches(self):
        ws.run_status("default", options(), now=NOW)
        self.store.requests.clear()
        ws.run_status("default", options(statuses="completed"), now=NOW + dt.timedelta(seconds=30))
        self.assertTrue(self.store.requests)

    def test_failed_settings_change_never_mixes_old_figures(self):
        ws.run_status("default", options(), now=NOW)
        self.store.fail_with = 500
        changed = options(statuses="completed")
        failed = ws.run_status("default", changed, now=NOW + dt.timedelta(seconds=30))
        self.assertEqual(failed["state"], "error")
        self.store.fail_with = None
        self.store.requests.clear()
        ws.run_status("default", changed, now=NOW + dt.timedelta(minutes=10))
        self.assertIn("/wp-json/", self.store.paths())

    def test_changing_only_the_bar_text_does_not_refetch(self):
        ws.run_status("default", options(), now=NOW)
        self.store.requests.clear()
        result = ws.run_status("default", options(barFormat="{orders}!"), now=NOW + dt.timedelta(seconds=30))
        self.assertEqual(self.store.requests, [])
        self.assertEqual(result["barText"], "1!")

    def test_pagination_reads_every_page(self):
        self.store.orders = [order(date=f"2026-08-15T09:{m % 60:02d}:{s:02d}", lines=[line(cost=1)])
                             for m in range(3) for s in range(60)] * 2
        result = ws.run_status("default", options(), now=NOW)
        self.assertEqual(result["periods"][0]["orders"], "360")
        pages = [q["page"] for p, q, _ in self.store.requests if p.endswith("/orders")]
        self.assertEqual(pages, ["1", "2", "3", "4"])

    def test_failure_keeps_last_figures_and_flags_them(self):
        ws.run_status("default", options(), now=NOW)
        self.store.fail_with = 500
        result = ws.run_status("default", options(), now=NOW + dt.timedelta(minutes=10))
        self.assertTrue(result["stale"])
        self.assertIn("500", result["error"])
        self.assertEqual(result["periods"][0]["revenue"], "£100.00")

    def test_failure_with_no_cache_is_an_error(self):
        self.store.fail_with = 500
        result = ws.run_status("default", options(), now=NOW)
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["errorKind"], "api")

    def test_failures_are_not_retried_by_every_monitor(self):
        self.store.fail_with = 500
        ws.run_status("default", options(), now=NOW)
        count = len(self.store.requests)
        result = ws.run_status("default", options(), now=NOW + dt.timedelta(seconds=5))
        self.assertEqual(len(self.store.requests), count)
        self.assertEqual(result["state"], "error")

    def test_force_bypasses_cache(self):
        ws.run_status("default", options(), now=NOW)
        self.store.requests.clear()
        ws.run_status("default", options(), force=True, now=NOW + dt.timedelta(seconds=1))
        self.assertIn("/wp-json/", self.store.paths())

    def test_product_and_variation_costs_are_looked_up_once(self):
        self.store.orders = [order(lines=[line(quantity=2, product=10), line(product=20, variation=77)])]
        result = ws.run_status("default", options(), now=NOW)
        self.assertEqual(result["periods"][0]["profit"], "£138.00")  # 200 - 2×25 - 12
        self.store.requests.clear()
        ws.run_status("default", options(), now=NOW + dt.timedelta(minutes=10))
        self.assertFalse([p for p in self.store.paths() if "products" in p])

    def test_uncosted_product_is_not_requested_every_poll(self):
        self.store.orders = [order(lines=[line(product=999)])]
        ws.run_status("default", options(), now=NOW)
        self.store.requests.clear()
        ws.run_status("default", options(), now=NOW + dt.timedelta(minutes=10))
        self.assertFalse([p for p in self.store.paths() if "products" in p])


class OtherStoreSetups(StoreTestCase):
    def status(self, **kwargs):
        return ws.run_status("default", options(), force=True, now=NOW, **kwargs)

    def test_custom_rest_prefix_is_discovered_and_remembered(self):
        self.store.rest_prefix = "/shop-api"
        result = self.status()
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["periods"][0]["revenue"], "£100.00")
        self.store.requests.clear()
        self.status()
        self.assertFalse([p for p in self.store.paths() if p.startswith("/wp-json")])

    def test_discovery_sends_no_credentials(self):
        self.store.rest_prefix = "/shop-api"
        self.status()
        self.assertTrue(self.store.home_requests)
        self.assertFalse([h for h in self.store.home_requests if "Authorization" in h])

    def test_plain_permalinks_work_where_woocommerce_accepts_keys(self):
        self.store.plain_permalinks = True
        result = self.status()
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["periods"][0]["orders"], "1")
        routes = [q.get("rest_route") for _, q, _ in self.store.requests]
        self.assertIn("/wc/v3/orders", routes)

    def test_older_woocommerce_on_plain_permalinks_explains_the_fix(self):
        self.store.plain_permalinks = True
        self.store.plain_rejects_keys = True
        result = self.status()
        self.assertEqual(result["errorKind"], "config")
        self.assertIn("Permalinks", result["error"])

    def test_api_root_on_another_host_is_never_given_credentials(self):
        other = MockStore()
        self.addCleanup(other.close)
        self.store.rest_prefix = "/shop-api"
        self.store.link_override = other.url + "/wp-json/"
        result = self.status()
        self.assertEqual(other.requests, [])
        self.assertEqual(result["state"], "error")

    def test_cached_api_root_on_another_host_is_ignored(self):
        client = ws.Client({"url": "https://shop.example.com", "key": "k", "secret": "s"}, "https://evil.example.net/wp-json/")
        self.assertIsNone(client.api_root)

    def test_https_store_will_not_be_downgraded_to_http(self):
        self.assertFalse(ws.same_origin("https://shop.example.com", "http://shop.example.com/wp-json/"))

    def test_missing_woocommerce_is_a_clear_error(self):
        self.store.rest_prefix = "/nothing"
        self.store.link_override = ""
        result = self.status()
        self.assertEqual(result["state"], "error")
        self.assertIn("WooCommerce API not found", result["error"])

    def test_hidden_public_details_use_the_latest_orders_offset(self):
        self.store.block_public_root = True
        self.store.orders = [order(lines=[line(cost=40)], date="2026-08-15T09:00:00", date_created="2026-08-15T10:00:00")]
        result = self.status()
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["store"]["name"], "127.0.0.1")
        self.assertIn("UTC+1", result["notice"])

    def test_hidden_details_with_a_half_hour_offset(self):
        self.store.block_public_root = True
        self.store.orders = [order(lines=[line(cost=40)], date="2026-08-15T09:00:00", date_created="2026-08-15T14:30:00")]
        self.assertIn("UTC+5.5", self.status()["notice"])

    def test_hidden_details_with_no_orders_use_utc_and_say_so(self):
        self.store.block_public_root = True
        self.store.orders = []
        result = self.status()
        self.assertEqual(result["state"], "ok")
        self.assertIn("days use UTC", result["notice"])

    def test_visible_details_have_no_notice(self):
        self.assertEqual(self.status()["notice"], "")

    def test_store_currency_settings_reach_the_output(self):
        self.store.settings = [
            {"id": "woocommerce_currency_pos", "value": "right_space"},
            {"id": "woocommerce_price_thousand_sep", "value": "."},
            {"id": "woocommerce_price_decimal_sep", "value": ","},
            {"id": "woocommerce_price_num_decimals", "value": "2"},
        ]
        self.store.orders = [order(total="1234.50", tax="0", lines=[line("1234.50", cost=1000)])]
        result = self.status()
        self.assertEqual(result["periods"][0]["revenue"], "1.234,50 £")
        self.assertEqual(result["barText"], "1.234 £|1|234 £|19%")
        self.assertEqual(result["trend"]["days"][-1]["detail"], "1.234,50 £ · 1 order · 234,50 £ profit")

    def test_unreadable_currency_settings_fall_back_to_defaults(self):
        self.store.deny_settings = True
        self.assertEqual(self.status()["periods"][0]["revenue"], "£100.00")

    def test_setup_error_never_contains_secrets_during_discovery(self):
        self.store.rest_prefix = "/nothing"
        self.store.link_override = ""
        self.assertNotIn(SECRET, json.dumps(self.status()))


class SelectablePeriods(StoreTestCase):
    def keys(self, result):
        return [period["key"] for period in result["periods"]]

    def after_values(self):
        return [q["after"] for p, q, _ in self.store.requests if p.endswith("/orders")]

    def test_defaults_match_the_original_breakdowns(self):
        result = ws.run_status("default", options(), now=NOW)
        self.assertEqual(self.keys(result), ["today", "yesterday", "7d", "mtd"])
        self.assertEqual(len(result["catalog"]), 13)
        self.assertEqual(len(result["trend"]["days"]), 30)

    def test_saved_prefs_choose_the_breakdowns(self):
        ws.save_prefs("default", {"periods": ["last_year", "today"], "trendDays": 7})
        result = ws.run_status("default", options(), now=NOW)
        self.assertEqual(self.keys(result), ["today", "last_year"])
        self.assertEqual(result["prefs"]["trendDays"], 7)
        self.assertEqual(len(result["trend"]["days"]), 7)

    def test_save_merges_with_existing_prefs_and_is_private(self):
        ws.save_prefs("default", {"trendDays": 90})
        prefs = ws.save_prefs("default", {"weekStart": "sunday"})
        self.assertEqual((prefs["trendDays"], prefs["weekStart"]), (90, "sunday"))
        self.assertEqual(stat.S_IMODE(os.stat(ws.prefs_path("default")).st_mode), 0o600)

    def test_prefs_are_per_store(self):
        ws.save_prefs("default", {"trendDays": 90})
        self.assertEqual(ws.load_prefs("other")["trendDays"], 30)

    def test_corrupt_prefs_file_falls_back_to_defaults(self):
        ws.save_prefs("default", {"trendDays": 90})
        with open(ws.prefs_path("default"), "w") as handle:
            handle.write("{not json")
        self.assertEqual(ws.run_status("default", options(), now=NOW)["state"], "ok")

    def test_selecting_a_longer_period_backfills_immediately(self):
        ws.run_status("default", options(), now=NOW)
        self.store.orders.append(order(date="2026-02-01T09:00:00", lines=[line(cost=10)]))
        self.store.requests.clear()
        ws.save_prefs("default", {"periods": ["ytd"]})
        result = ws.run_status("default", options(), now=NOW + dt.timedelta(seconds=10))
        self.assertEqual(self.after_values(), ["2025-12-31T23:59:59"])
        self.assertEqual(result["periods"][0]["orders"], "2")

    def test_removing_a_period_does_not_refetch(self):
        ws.save_prefs("default", {"periods": ["ytd"]})
        ws.run_status("default", options(), now=NOW)
        self.store.requests.clear()
        ws.save_prefs("default", {"periods": ["today"]})
        ws.run_status("default", options(), now=NOW + dt.timedelta(seconds=10))
        self.assertEqual(self.store.requests, [])

    def test_hourly_refresh_stays_recent_and_keeps_older_days(self):
        self.store.orders.append(order(date="2026-02-01T09:00:00", lines=[line(cost=10)]))
        ws.save_prefs("default", {"periods": ["ytd"]})
        ws.run_status("default", options(), now=NOW)
        self.store.requests.clear()
        result = ws.run_status("default", options(), now=NOW + dt.timedelta(minutes=61))
        self.assertEqual(self.after_values(), ["2026-07-10T22:59:59"])  # 35 days back, midnight BST
        self.assertEqual(result["periods"][0]["orders"], "2")

    def test_full_range_refreshes_daily(self):
        ws.save_prefs("default", {"periods": ["ytd"]})
        ws.run_status("default", options(), now=NOW)
        self.store.requests.clear()
        ws.run_status("default", options(), now=NOW + dt.timedelta(hours=25))
        self.assertEqual(self.after_values(), ["2025-12-31T23:59:59"])

    def test_late_refund_on_a_recent_day_is_picked_up_hourly(self):
        recent = order(date="2026-08-10T09:00:00", lines=[line(cost=10)])
        self.store.orders = [recent]
        ws.run_status("default", options(), now=NOW)
        recent["refunds"] = [{"total": "-120.00"}]
        result = ws.run_status("default", options(), now=NOW + dt.timedelta(minutes=61))
        week = {p["key"]: p for p in result["periods"]}["7d"]
        self.assertEqual(week["revenue"], "£0.00")

    def test_failed_backfill_shows_unknown_not_a_partial_total(self):
        ws.run_status("default", options(), now=NOW)
        self.store.fail_with = 500
        ws.save_prefs("default", {"periods": ["today", "ytd"]})
        result = ws.run_status("default", options(), now=NOW + dt.timedelta(seconds=10))
        byKey = {p["key"]: p for p in result["periods"]}
        self.assertTrue(result["stale"])
        self.assertEqual(byKey["today"]["revenue"], "£100.00")
        self.assertEqual(byKey["ytd"]["revenue"], "—")
        self.assertIn("not loaded", byKey["ytd"]["note"])

    def test_unknown_bar_period_falls_back_to_today(self):
        result = ws.run_status("default", options(barPeriod="nope"), now=NOW)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["barText"], "£100|1|£60|60%")


class Security(StoreTestCase):
    def outputs(self):
        return json.dumps(ws.run_status("default", options(), force=True, now=NOW))

    def test_secrets_never_reach_output_or_cache(self):
        output = self.outputs()
        with open(ws.cache_path("default")) as handle:
            cached = handle.read()
        for text in (output, cached):
            self.assertNotIn(KEY, text)
            self.assertNotIn(SECRET, text)

    def test_cache_has_no_customer_details(self):
        self.store.orders = [order(lines=[line(cost=1)], billing={"email": "someone@example.com"})]
        self.outputs()
        with open(ws.cache_path("default")) as handle:
            self.assertNotIn("someone@example.com", handle.read())

    def test_order_request_does_not_ask_for_customer_fields(self):
        self.outputs()
        fields = [q["_fields"] for p, q, _ in self.store.requests if p.endswith("/orders")][0]
        for private in ("billing", "shipping", "customer", "ip"):
            self.assertNotIn(private, fields)

    def test_cache_is_private(self):
        self.outputs()
        self.assertEqual(stat.S_IMODE(os.stat(ws.cache_path("default")).st_mode), 0o600)

    def test_basic_auth_preferred_and_query_auth_only_after_401(self):
        self.outputs()
        queries = [q for p, q, _ in self.store.requests if "consumer_secret" in q]
        self.assertEqual(queries, [])
        self.store.requests.clear()
        self.store.strip_basic_auth = True
        result = ws.run_status("default", options(), force=True, now=NOW)
        self.assertEqual(result["state"], "ok")

    def test_redirect_is_not_followed_with_credentials(self):
        other = MockStore()
        self.addCleanup(other.close)
        self.store.redirect_to = other.url + "/wp-json/"
        result = ws.run_status("default", options(), force=True, now=NOW)
        self.assertEqual(other.requests, [])
        self.assertEqual(result["errorKind"], "config")
        self.assertIn("redirects", result["error"])

    def test_errors_do_not_leak_credentials(self):
        self.store.strip_basic_auth = True
        self.store.fail_with = None
        client = ws.Client({"url": self.store.url, "key": "wrong", "secret": "alsowrong"})
        with self.assertRaises(ws.WooError) as caught:
            client.get("wc/v3/orders")
        self.assertEqual(caught.exception.kind, "auth")
        self.assertNotIn("alsowrong", caught.exception.message)

    def test_plain_http_is_refused_except_loopback(self):
        with self.assertRaises(ws.WooError):
            ws.normalize_url("http://shop.example.com")
        self.assertEqual(ws.normalize_url("shop.example.com/"), "https://shop.example.com")
        self.assertEqual(ws.normalize_url("http://127.0.0.1:8080"), "http://127.0.0.1:8080")

    def test_loose_config_permissions_are_refused(self):
        os.chmod(ws.config_path("default"), 0o644)
        result = ws.run_status("default", options(), now=NOW)
        self.assertEqual(result["errorKind"], "config")
        self.assertEqual(self.store.requests, [])

    def test_profile_cannot_escape_config_dir(self):
        result = ws.run_status("../../etc/passwd", options(), now=NOW)
        self.assertEqual(result["errorKind"], "config")


if __name__ == "__main__":
    unittest.main()
