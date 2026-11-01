#!/usr/bin/env python3
"""WooCommerce revenue, profit and order stats for the Omarchy bar.

  woo_stats.py status   Print one JSON snapshot for the widget. Always exits 0.
  woo_stats.py setup    Prompt for store URL and API key, then save them.
  woo_stats.py doctor   Report where this store's cost data comes from.
  woo_stats.py prefs    Change what the panel shows, for example:
                        prefs --set '{"periods": ["today", "30d"], "trendDays": 90}'

Standard library only. The cache holds daily totals and product costs, never
customer details, and credentials never appear in output, argv or the cache.
"""

import argparse
import base64
import datetime as dt
import fcntl
import getpass
import hashlib
import html
import ipaddress
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

APP = "omarchy-woocommerce"
USER_AGENT = "omarchy-woocommerce/1.0"
HTTP_TIMEOUT = 20
PER_PAGE = 100
MAX_PAGES = 500
PRODUCT_COST_TTL = 24 * 3600
CACHE_VERSION = 2
RECENT_DAYS = 35
DEEP_REFRESH_SECONDS = 24 * 3600
FULL_COVERAGE = 0.995
DEFAULT_BAR_FORMAT = "󰄐 {revenue} · {orders} · {margin}"

ORDER_FIELDS = (
    "id,status,total,total_tax,currency,date_created_gmt,refunds,"
    "line_items,meta_data,cost_of_goods_sold"
)

# Cost meta written by the common cost plugins. Core WooCommerce COGS needs no
# entry here because it has its own `cost_of_goods_sold` REST field.
ORDER_COST_KEYS = ("_wc_cog_order_total_cost", "_alg_wc_cog_order_cost")
LINE_TOTAL_COST_KEYS = ("_wc_cog_item_total_cost",)
LINE_UNIT_COST_KEYS = ("_wc_cog_item_cost", "_alg_wc_cog_item_cost")
PRODUCT_COST_KEYS = ("_wc_cog_cost", "_alg_wc_cog_cost")

PERIODS = (
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("wtd", "This week"),
    ("last_week", "Last week"),
    ("7d", "Last 7 days"),
    ("30d", "Last 30 days"),
    ("90d", "Last 90 days"),
    ("mtd", "This month"),
    ("last_month", "Last month"),
    ("qtd", "This quarter"),
    ("last_quarter", "Last quarter"),
    ("ytd", "This year"),
    ("last_year", "Last year"),
)
PERIOD_LABELS = dict(PERIODS)
DEFAULT_PREFS = {
    "periods": ["today", "yesterday", "7d", "mtd"],
    "trendDays": 30,
    "trendMetric": "revenue",
    "weekStart": "monday",
}
TREND_DAYS = (0, 7, 14, 30, 90)
TREND_METRICS = ("revenue", "profit", "orders")
WEEK_STARTS = {"monday": 0, "sunday": 6}


class WooError(Exception):
    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


class NotConfigured(WooError):
    def __init__(self, message="Store is not set up yet"):
        super().__init__("not_configured", message)


# --- Paths and files ---------------------------------------------------------

def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP)


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP)


def checked_profile(profile):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile or ""):
        raise WooError("config", "Profile names may only use letters, digits, - and _")
    return profile


def config_path(profile):
    return os.path.join(config_dir(), checked_profile(profile) + ".json")


def prefs_path(profile):
    return os.path.join(config_dir(), checked_profile(profile) + ".prefs.json")


def cache_path(profile):
    return os.path.join(cache_dir(), checked_profile(profile) + ".json")


def write_private_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


# --- Configuration and credentials -------------------------------------------

def normalize_url(raw):
    url = str(raw or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        raise WooError("config", "Store URL is not valid")
    if parts.scheme != "https" and not is_loopback(parts.hostname):
        raise WooError("config", "Store URL must use https, because API keys are sent with every request")
    return url


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def secret_lookup(profile, field):
    if not shutil.which("secret-tool"):
        raise WooError("auth", "secret-tool is not installed, but this profile uses the keyring")
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", APP, "profile", profile, "field", field],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise WooError("auth", "Timed out reading the keyring. It may be locked") from exc
    if result.returncode != 0 or not result.stdout:
        raise WooError("auth", "API key not found in the keyring. Run setup again")
    return result.stdout.strip()


def secret_store(profile, field, value):
    subprocess.run(
        ["secret-tool", "store", "--label", f"WooCommerce {profile} {field}",
         "application", APP, "profile", profile, "field", field],
        input=value, text=True, check=True, capture_output=True, timeout=30,
    )


def load_config(profile):
    path = config_path(profile)
    if not os.path.exists(path):
        raise NotConfigured()
    if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
        raise WooError("config", f"{path} is readable by other users. Run: chmod 600 {path}")
    try:
        with open(path) as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise WooError("config", f"Could not read {path}") from exc
    url = normalize_url(config.get("url"))
    if config.get("credentialStore") == "keyring":
        key = secret_lookup(profile, "consumer_key")
        secret = secret_lookup(profile, "consumer_secret")
    else:
        key = config.get("consumerKey")
        secret = config.get("consumerSecret")
    if not key or not secret:
        raise NotConfigured("API key is missing. Run setup again")
    return {"url": url, "key": key, "secret": secret}


# --- Panel preferences -------------------------------------------------------
#
# Saved per store here, not in shell.json. The shell's settings update replaces
# every widget with the same id, so two stores would overwrite each other.

def normalize_prefs(raw):
    raw = raw if isinstance(raw, dict) else {}
    chosen = raw.get("periods")
    chosen = chosen if isinstance(chosen, list) else []
    periods = [key for key, _ in PERIODS if key in chosen]
    days = raw.get("trendDays")
    metric = raw.get("trendMetric")
    week = raw.get("weekStart")
    return {
        "periods": periods or list(DEFAULT_PREFS["periods"]),
        "trendDays": days if days in TREND_DAYS else DEFAULT_PREFS["trendDays"],
        "trendMetric": metric if metric in TREND_METRICS else DEFAULT_PREFS["trendMetric"],
        "weekStart": week if week in WEEK_STARTS else DEFAULT_PREFS["weekStart"],
    }


def load_prefs(profile):
    try:
        with open(prefs_path(profile)) as handle:
            return normalize_prefs(json.load(handle))
    except (OSError, ValueError):
        return normalize_prefs({})


def save_prefs(profile, changes):
    if not isinstance(changes, dict):
        raise WooError("config", "Preferences must be a JSON object")
    prefs = normalize_prefs({**load_prefs(profile), **changes})
    write_private_json(prefs_path(profile), prefs)
    return prefs


# --- HTTP --------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib keeps the Authorization header across redirects, including to
    # other hosts, so a redirect must never be followed with credentials.
    def redirect_request(self, *args, **kwargs):
        return None


def same_origin(url, other):
    first, second = urllib.parse.urlsplit(url), urllib.parse.urlsplit(other)
    return (first.scheme, first.hostname, first.port) == (second.scheme, second.hostname, second.port)


def link_api_root(headers):
    """Return the REST API address WordPress publishes in its Link header."""
    for header in headers.get_all("Link") or []:
        match = re.search(r'<([^>]+)>\s*;\s*rel="https://api\.w\.org/"', header)
        if match:
            return match.group(1)
    return None


class Client:
    def __init__(self, config, api_root=None):
        self.base = config["url"]
        self.key = config["key"]
        self.secret = config["secret"]
        self.query_auth = False
        self.discovered = False
        self.api_root = api_root if api_root and same_origin(self.base, api_root) else None
        self.opener = urllib.request.build_opener(_NoRedirect)

    @property
    def plain_permalinks(self):
        return bool(self.api_root and "rest_route=" in self.api_root)

    def _url(self, path, params):
        root = self.api_root or f"{self.base}/wp-json/"
        params = dict(params)
        if self.plain_permalinks:
            root = root.split("?")[0]
            params = {"rest_route": "/" + path, **params}
        else:
            root += path
        return root + ("?" + urllib.parse.urlencode(params) if params else "")

    def _open(self, path, params, authenticated):
        params = dict(params or {})
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if authenticated and self.query_auth:
            params.update(consumer_key=self.key, consumer_secret=self.secret)
        elif authenticated:
            token = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
            headers["Authorization"] = "Basic " + token
        request = urllib.request.Request(self._url(path, params), headers=headers)
        return self.opener.open(request, timeout=HTTP_TIMEOUT)

    def _request(self, path, params, authenticated):
        """Return (decoded JSON, headers). Error text never includes the URL."""
        try:
            try:
                response = self._open(path, params, authenticated)
            except urllib.error.HTTPError as exc:
                # Some hosts strip the Authorization header. Only then is it
                # worth retrying with WooCommerce's query-string credentials.
                if exc.code != 401 or not authenticated or self.query_auth:
                    raise
                exc.close()
                self.query_auth = True
                response = self._open(path, params, authenticated)
            with response:
                body = response.read()
                headers = response.headers
        except urllib.error.HTTPError as exc:
            exc.close()
            raise http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise WooError("network", f"Could not reach the store: {reason}") from None
        try:
            return json.loads(body), headers
        except ValueError:
            raise WooError("api", "The store returned something other than JSON. "
                                  "A firewall or challenge page may be blocking the API",
                           status="not_json") from None

    def discover_api_root(self):
        """Ask the home page where the API lives. Sends no credentials."""
        self.discovered = True
        request = urllib.request.Request(self.base + "/", headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT) as response:
                root = link_api_root(response.headers)
        except urllib.error.HTTPError as exc:
            exc.close()
            root = link_api_root(exc.headers)
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
        # The API address comes from the site's response, and API keys are about
        # to be sent to it, so it has to be the store we were configured with.
        if not root or not same_origin(self.base, root):
            return False
        current = self.api_root or f"{self.base}/wp-json/"
        if root == current:
            return False
        self.api_root = root
        return True

    def get(self, path, params=None, authenticated=True):
        try:
            try:
                return self._request(path, params, authenticated)
            except WooError as error:
                if error.status not in (404, "not_json") or self.discovered or not self.discover_api_root():
                    raise
            return self._request(path, params, authenticated)
        except WooError as error:
            # WooCommerce only accepts API keys on plain permalinks from 11.1.
            if error.status == 401 and self.plain_permalinks:
                raise WooError("config", "This store uses plain permalinks, and its WooCommerce version "
                                         "can't accept API keys that way. Choose any other option under "
                                         "WordPress Settings > Permalinks", status=401) from None
            raise


def http_error(exc):
    code = exc.code
    if code in (301, 302, 303, 307, 308):
        target = exc.headers.get("Location", "")
        origin = urllib.parse.urlsplit(target)
        where = f"{origin.scheme}://{origin.netloc}" if origin.netloc else "another address"
        return WooError("config", f"Store URL redirects to {where}. Run setup with that address")
    if code == 401:
        return WooError("auth", "The store rejected the API key. Check it or create a new one", status=401)
    if code == 403:
        return WooError("auth", "Access denied. The key needs Read permission, or a firewall is blocking it", status=403)
    if code == 404:
        return WooError("api", "WooCommerce API not found at this address. "
                               "Check the URL, and that WooCommerce is active", status=404)
    if code == 429:
        return WooError("network", "The store is rate limiting requests. Try a longer refresh interval")
    return WooError("api", f"The store returned HTTP {code}")


# --- Number helpers ----------------------------------------------------------

def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result and abs(result) != float("inf") else 0.0


def meta_map(meta_data):
    result = {}
    if isinstance(meta_data, list):
        for entry in meta_data:
            if isinstance(entry, dict) and "key" in entry:
                result[entry["key"]] = entry.get("value")
    return result


def first_cost(meta, keys):
    for key in keys:
        value = number(meta.get(key))
        if value > 0:
            return value
    return 0.0


# --- Cost resolution ---------------------------------------------------------

def line_cost(line, options, product_costs):
    """Return (total cost for this line, source). Zero means cost is unknown."""
    core = number((line.get("cost_of_goods_sold") or {}).get("value"))
    if core > 0:
        return core, "core"
    quantity = number(line.get("quantity")) or 1
    meta = meta_map(line.get("meta_data"))
    total = first_cost(meta, LINE_TOTAL_COST_KEYS)
    if total > 0:
        return total, "line-meta"
    unit = first_cost(meta, tuple(options["costMetaKeys"]) + LINE_UNIT_COST_KEYS)
    if unit > 0:
        return unit * quantity, "line-meta"
    unit = number((product_costs.get(product_key(line)) or {}).get("cost"))
    if unit > 0:
        return unit * quantity, "product"
    return 0.0, "none"


def product_key(line):
    variation = int(number(line.get("variation_id")))
    product = int(number(line.get("product_id")))
    return f"{product}:{variation}"


def product_unit_cost(product, options):
    core = number((product.get("cost_of_goods_sold") or {}).get("total_value"))
    if core > 0:
        return core
    keys = tuple(options["costMetaKeys"]) + PRODUCT_COST_KEYS
    return first_cost(meta_map(product.get("meta_data")), keys)


def order_cost(order):
    core = number((order.get("cost_of_goods_sold") or {}).get("total_value"))
    if core > 0:
        return core
    return first_cost(meta_map(order.get("meta_data")), ORDER_COST_KEYS)


def load_product_costs(client, orders, options, cached, now_ts):
    """Look up costs for lines the order itself has no cost for."""
    fresh = {k: v for k, v in (cached or {}).items() if now_ts - v.get("at", 0) < PRODUCT_COST_TTL}
    needed = set()
    for order in orders:
        if order_cost(order) > 0:
            continue
        for line in order.get("line_items") or []:
            key = product_key(line)
            if key != "0:0" and key not in fresh and line_cost(line, options, {})[0] <= 0:
                needed.add(key)

    simple = sorted({k.split(":")[0] for k in needed if k.endswith(":0")})
    for start in range(0, len(simple), PER_PAGE):
        batch = simple[start:start + PER_PAGE]
        products, _ = client.get("wc/v3/products", {
            "include": ",".join(batch), "per_page": PER_PAGE, "_fields": "id,cost_of_goods_sold,meta_data",
        })
        for product in products:
            fresh[f"{product.get('id')}:0"] = {"cost": product_unit_cost(product, options), "at": now_ts}

    by_parent = {}
    for key in needed:
        parent, variation = key.split(":")
        if variation != "0":
            by_parent.setdefault(parent, []).append(variation)
    for parent, variations in sorted(by_parent.items()):
        variations = sorted(variations)
        for start in range(0, len(variations), PER_PAGE):
            batch = variations[start:start + PER_PAGE]
            items, _ = client.get(f"wc/v3/products/{parent}/variations", {
                "include": ",".join(batch), "per_page": PER_PAGE, "_fields": "id,cost_of_goods_sold,meta_data",
            })
            for item in items:
                fresh[f"{parent}:{item.get('id')}"] = {"cost": product_unit_cost(item, options), "at": now_ts}

    # Remember misses too, so an uncosted product is not re-requested every poll.
    for key in needed:
        fresh.setdefault(key, {"cost": 0, "at": now_ts})
    return fresh


# --- Aggregation -------------------------------------------------------------

def empty_day():
    return {"orders": 0, "gross": 0.0, "net": 0.0, "productNet": 0.0,
            "costedNet": 0.0, "assumedNet": 0.0, "cost": 0.0, "otherCurrency": 0}


def summarize_order(order, options, product_costs):
    total = number(order.get("total"))
    tax = number(order.get("total_tax"))
    refunded = sum(abs(number(r.get("total"))) for r in order.get("refunds") or [] if isinstance(r, dict))
    # Refunds report one total including tax, so tax and cost scale with it.
    kept = 1.0 - min(1.0, refunded / total) if total > 0 else 1.0
    # A cost recorded on the order beats today's product cost.
    whole_order = order_cost(order)
    if whole_order > 0:
        product_costs = {}

    product_net = costed_net = assumed_net = cost = 0.0
    uncosted = []
    for line in order.get("line_items") or []:
        line_net = number(line.get("total"))
        product_net += line_net
        value, _ = line_cost(line, options, product_costs)
        if value > 0:
            costed_net += line_net
            cost += value
        else:
            uncosted.append(line_net)

    # An order-level cost only stands in when no line has a cost of its own.
    if whole_order > 0 and cost <= 0:
        costed_net = product_net
        cost = whole_order
        uncosted = []

    margin = options["assumedMargin"]
    if margin > 0:
        for line_net in uncosted:
            assumed_net += line_net
            cost += line_net * (1 - margin / 100.0)

    return {
        "orders": 1,
        "gross": max(0.0, total - refunded),
        "net": (total - tax) * kept,
        "productNet": product_net * kept,
        "costedNet": costed_net * kept,
        "assumedNet": assumed_net * kept,
        "cost": cost * kept,
        "otherCurrency": 0,
    }


def add_day(target, values):
    for key, value in values.items():
        target[key] = target.get(key, 0) + value


def order_date(order, tz):
    raw = str(order.get("date_created_gmt") or "")
    try:
        created = dt.datetime.fromisoformat(raw).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return created.astimezone(tz).date().isoformat()


def aggregate(orders, tz, currency, options, product_costs):
    days = {}
    for order in orders:
        day = order_date(order, tz)
        if day is None:
            continue
        bucket = days.setdefault(day, empty_day())
        if currency and order.get("currency") and order["currency"] != currency:
            bucket["otherCurrency"] += 1
            continue
        add_day(bucket, summarize_order(order, options, product_costs))
    return days


def quarter_start(date):
    return date.replace(month=(date.month - 1) // 3 * 3 + 1, day=1)


def period_range(key, today, week_start=0):
    """Return the (first, last) dates of a period, inclusive."""
    day = dt.timedelta(days=1)
    this_week = today - dt.timedelta(days=(today.weekday() - week_start) % 7)
    this_month = today.replace(day=1)
    this_quarter = quarter_start(today)
    if key == "yesterday":
        return today - day, today - day
    if key == "wtd":
        return this_week, today
    if key == "last_week":
        return this_week - 7 * day, this_week - day
    if key == "7d":
        return today - 6 * day, today
    if key == "30d":
        return today - 29 * day, today
    if key == "90d":
        return today - 89 * day, today
    if key == "mtd":
        return this_month, today
    if key == "last_month":
        return (this_month - day).replace(day=1), this_month - day
    if key == "qtd":
        return this_quarter, today
    if key == "last_quarter":
        return quarter_start(this_quarter - day), this_quarter - day
    if key == "ytd":
        return today.replace(month=1, day=1), today
    if key == "last_year":
        return dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31)
    return today, today


def period_days(key, today, week_start=0):
    first, last = period_range(key, today, week_start)
    return [first + dt.timedelta(days=n) for n in range((last - first).days + 1)]


def needed_start(options, today):
    """The earliest date any selected period, the bar or the trend needs."""
    week_start = WEEK_STARTS[options["weekStart"]]
    keys = list(options["periods"]) + [options["barPeriod"]]
    start = min(period_range(key, today, week_start)[0] for key in keys)
    if options["trendDays"]:
        start = min(start, today - dt.timedelta(days=options["trendDays"] - 1))
    return min(start, today)


def period_metrics(days, dates, covered_from=None):
    if covered_from and dates and min(dates).isoformat() < covered_from:
        # A failed fetch can leave earlier days missing. A partial total here
        # would look like a real, lower figure.
        return {"loading": True, "orders": None, "revenue": None, "gross": None, "aov": None, "profit": None,
                "margin": None, "coverage": None, "assumed": False, "otherCurrency": 0}
    total = empty_day()
    for date in dates:
        add_day(total, days.get(date.isoformat(), {}))
    orders = int(total["orders"])
    known = total["costedNet"] + total["assumedNet"]
    has_cost = total["cost"] > 0 or known > 0
    profit = (known - total["cost"]) if has_cost else (0.0 if orders == 0 else None)
    return {
        "loading": False,
        "orders": orders,
        "revenue": total["net"],
        "gross": total["gross"],
        "aov": total["net"] / orders if orders else None,
        "profit": profit,
        "margin": (known - total["cost"]) / known * 100 if known > 0 else None,
        "coverage": min(1.0, known / total["productNet"]) if total["productNet"] > 0 else None,
        "assumed": total["assumedNet"] > 0,
        "otherCurrency": int(total["otherCurrency"]),
    }


# --- Display -----------------------------------------------------------------

DEFAULT_FORMAT = {"position": "left", "thousand": ",", "decimal": ".", "decimals": 2}


def as_format(currency):
    return currency if isinstance(currency, dict) else {**DEFAULT_FORMAT, "symbol": currency}


def format_number(amount, decimals, fmt):
    whole, _, fraction = f"{amount:,.{decimals}f}".partition(".")
    whole = whole.replace(",", fmt["thousand"])
    return whole + fmt["decimal"] + fraction if fraction else whole


def money(value, currency, compact=False):
    """Format an amount the way the store's own currency settings do."""
    if value is None:
        return "—"
    fmt = as_format(currency)
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if compact and round(amount) >= 100000:
        text = format_number(amount / 1000, 0, fmt) + "k"
    elif compact:
        text = format_number(amount, 0, fmt)
    else:
        text = format_number(amount, fmt["decimals"], fmt)
    symbol = fmt["symbol"]
    placed = {
        "right": f"{text}{symbol}",
        "left_space": f"{symbol} {text}",
        "right_space": f"{text} {symbol}",
    }.get(fmt["position"], f"{symbol}{text}")
    return sign + placed


def percent(value):
    return "—" if value is None else f"{value:.0f}%"


def count(value):
    return "—" if value is None else str(value)


def nice_ceiling(value):
    """Round up to a clean axis label that still leaves the chart well filled."""
    if value <= 1:
        return 1
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 3, 4, 5, 6, 8, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def is_estimate(metrics):
    coverage = metrics["coverage"]
    return metrics["assumed"] or (coverage is not None and coverage < FULL_COVERAGE)


def bar_tokens(metrics, currency, label):
    tilde = "~" if is_estimate(metrics) and metrics["profit"] is not None else ""
    return {
        "period": label,
        "revenue": money(metrics["revenue"], currency, compact=True),
        "gross": money(metrics["gross"], currency, compact=True),
        "profit": tilde + money(metrics["profit"], currency, compact=True),
        "margin": tilde + percent(metrics["margin"]),
        "orders": count(metrics["orders"]),
        "aov": money(metrics["aov"], currency, compact=True),
    }


def render_bar(template, tokens):
    return re.sub(r"\{(\w+)\}", lambda m: tokens.get(m.group(1), m.group(0)), template)


def coverage_note(metrics):
    if metrics["loading"]:
        return "Some periods are not loaded yet, so they show —"
    coverage = metrics["coverage"]
    if metrics["otherCurrency"]:
        return f"{metrics['otherCurrency']} orders in another currency are not counted"
    if metrics["orders"] == 0 or coverage is None:
        return ""
    if metrics["profit"] is None:
        return "No cost data on these orders, so profit is unknown"
    if metrics["assumed"]:
        return "Includes your assumed margin for products without a cost"
    if coverage < FULL_COVERAGE:
        return f"Profit only counts the {coverage * 100:.0f}% of sales with known costs"
    return ""


def profit_text(metrics, currency, compact=False):
    tilde = "~" if is_estimate(metrics) and metrics["profit"] is not None else ""
    return tilde + money(metrics["profit"], currency, compact=compact)


def trend_axis(values, formatter):
    known = [value for value in values if value is not None]
    high = max(known, default=0)
    low = min(known, default=0)
    top = nice_ceiling(high) if high > 0 else 0
    bottom = -nice_ceiling(-low) if low < 0 else 0
    return {
        "top": top or (0 if bottom else 1),
        "bottom": bottom,
        "topText": formatter(top) if top else "",
        "bottomText": formatter(bottom) if bottom else "",
    }


def build_trend(days, today, options, currency, covered_from):
    length = options["trendDays"]
    if not length:
        return None
    rows = []
    for offset in range(length - 1, -1, -1):
        date = today - dt.timedelta(days=offset)
        metrics = period_metrics(days, [date], covered_from)
        orders = metrics["orders"]
        noun = "order" if orders == 1 else "orders"
        rows.append({
            "date": date.isoformat(),
            "label": "Today so far" if offset == 0 else f"{date:%a} {date.day} {date:%b}",
            "short": "Today" if offset == 0 else f"{date.day} {date:%b}",
            "today": offset == 0,
            "revenue": metrics["revenue"],
            "profit": metrics["profit"],
            "orders": orders,
            "detail": f"{money(metrics['revenue'], currency)} · {count(orders)} {noun} · "
                      f"{profit_text(metrics, currency)} profit",
        })
    first = today - dt.timedelta(days=length - 1)
    dates = [first + dt.timedelta(days=n) for n in range(length)]
    total = period_metrics(days, dates, covered_from)
    money_label = lambda value: money(value, currency, compact=True)
    return {
        "label": f"Last {length} days",
        "days": rows,
        "totals": {
            "revenue": money(total["revenue"], currency),
            "profit": profit_text(total, currency),
            "orders": count(total["orders"]),
        },
        "axes": {
            "revenue": trend_axis([row["revenue"] for row in rows], money_label),
            "profit": trend_axis([row["profit"] for row in rows], money_label),
            "orders": trend_axis([row["orders"] for row in rows], lambda value: format_number(value, 0, as_format(currency))),
        },
    }


def snapshot(cache, profile, options, now, stale_error=None):
    store = cache["store"]
    tz = store_timezone(store)
    today = now.astimezone(tz).date()
    currency = store_format(store)
    days = cache.get("days", {})

    covered_from = cache.get("coveredFrom")
    week_start = WEEK_STARTS[options["weekStart"]]

    periods = []
    for key in options["periods"]:
        metrics = period_metrics(days, period_days(key, today, week_start), covered_from)
        note = coverage_note(metrics)
        if note and not metrics["loading"]:
            note = f"{PERIOD_LABELS[key]}: {note[0].lower()}{note[1:]}"
        periods.append({
            "key": key,
            "label": PERIOD_LABELS[key],
            "revenue": money(metrics["revenue"], currency),
            "gross": money(metrics["gross"], currency),
            "orders": count(metrics["orders"]),
            "aov": money(metrics["aov"], currency),
            "profit": profit_text(metrics, currency),
            "margin": percent(metrics["margin"]),
            "note": note,
        })

    bar_key = options["barPeriod"]
    metrics = period_metrics(days, period_days(bar_key, today, week_start), covered_from)
    tokens = bar_tokens(metrics, currency, PERIOD_LABELS[bar_key])
    updated = dt.datetime.fromtimestamp(cache.get("todayAt", 0), dt.timezone.utc).astimezone()
    return {
        "state": "ok",
        "stale": bool(stale_error),
        "error": stale_error or "",
        "store": {
            "name": html.unescape(str(store.get("name") or "")) or urllib.parse.urlsplit(store["url"]).hostname,
            "url": store["url"],
            "adminUrl": store["url"] + "/wp-admin/admin.php?page=wc-admin&path=/analytics/overview",
            "currency": store.get("currency", ""),
        },
        "updated": updated.strftime("%H:%M"),
        "barText": render_bar(options["barFormat"], tokens),
        "tooltip": f"{tokens['period']}: {tokens['revenue']}, {tokens['orders']} orders, "
                   f"profit {tokens['profit']} ({tokens['margin']})",
        "notice": timezone_notice(store),
        "periods": periods,
        "trend": build_trend(days, today, options, currency, covered_from),
        "prefs": {key: options[key] for key in DEFAULT_PREFS},
        "catalog": [{"key": key, "label": label} for key, label in PERIODS],
    }


def error_output(error):
    return {"state": "not_configured" if isinstance(error, NotConfigured) else "error",
            "stale": False, "errorKind": error.kind, "error": error.message,
            "barText": "", "tooltip": error.message, "periods": []}


# --- Fetching ----------------------------------------------------------------

def store_timezone(store):
    name = store.get("timezone")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return dt.timezone(dt.timedelta(hours=number(store.get("gmtOffset"))))


def fetch_public_details(client):
    """Name and timezone, or None where a security plugin hides this endpoint."""
    try:
        root, _ = client.get("", {"_fields": "name,timezone_string,gmt_offset"}, authenticated=False)
    except WooError as error:
        if error.kind in ("auth", "api"):
            return None
        raise
    return root if isinstance(root, dict) else None


def offset_from_latest_order(client):
    """Each order has local and GMT times, and the gap is the store's UTC offset."""
    orders, _ = client.get("wc/v3/orders", {"per_page": 1, "_fields": "date_created,date_created_gmt"})
    if not orders:
        return None
    try:
        local = dt.datetime.fromisoformat(orders[0]["date_created"])
        gmt = dt.datetime.fromisoformat(orders[0]["date_created_gmt"])
    except (KeyError, TypeError, ValueError):
        return None
    return round((local - gmt).total_seconds() / 900) / 4


def fetch_currency_format(client):
    """Separators and symbol position. Defaults apply if the key can't read them."""
    try:
        rows, _ = client.get("wc/v3/settings/general", {"_fields": "id,value"})
    except WooError as error:
        if error.kind in ("auth", "api"):
            return {}
        raise
    values = {row.get("id"): row.get("value") for row in rows if isinstance(row, dict)} if isinstance(rows, list) else {}
    fmt = {}
    position = values.get("woocommerce_currency_pos")
    if position in ("left", "right", "left_space", "right_space"):
        fmt["position"] = position
    if values.get("woocommerce_price_thousand_sep") is not None:
        fmt["thousand"] = html.unescape(str(values["woocommerce_price_thousand_sep"]))
    if values.get("woocommerce_price_decimal_sep"):
        fmt["decimal"] = html.unescape(str(values["woocommerce_price_decimal_sep"]))
    if values.get("woocommerce_price_num_decimals") not in (None, ""):
        fmt["decimals"] = max(0, min(6, int(number(values["woocommerce_price_num_decimals"]))))
    return fmt


def fetch_store(client):
    public = fetch_public_details(client)
    currency, _ = client.get("wc/v3/data/currencies/current")
    store = {
        "url": client.base,
        "name": (public or {}).get("name", ""),
        "timezone": (public or {}).get("timezone_string") or "",
        "gmtOffset": number((public or {}).get("gmt_offset")),
        "timezoneSource": "store",
        "currency": currency.get("code", ""),
        "symbol": html.unescape(str(currency.get("symbol") or "")),
        "format": fetch_currency_format(client),
    }
    if public is None:
        offset = offset_from_latest_order(client)
        store.update(gmtOffset=offset or 0.0, timezoneSource="default" if offset is None else "orders")
    return store


def store_format(store):
    fmt = {**DEFAULT_FORMAT, **(store.get("format") or {})}
    fmt["symbol"] = store.get("symbol") or ""
    if not fmt["symbol"]:
        fmt.update(symbol=store.get("currency", ""), position="left_space")
    return fmt


def timezone_notice(store):
    source = store.get("timezoneSource", "store")
    if source == "store":
        return ""
    offset = number(store.get("gmtOffset"))
    label = f"UTC{offset:+g}" if offset else "UTC"
    if source == "orders":
        return (f"This store hides its timezone, so days use {label}, taken from the latest order. "
                "They may be an hour out after a clock change")
    return f"This store hides its timezone, so days use {label}"


def fetch_orders(client, start, statuses):
    """All orders created on or after `start`, an aware datetime."""
    after = (start - dt.timedelta(seconds=1)).astimezone(dt.timezone.utc)
    orders = []
    page = 1
    while True:
        batch, headers = client.get("wc/v3/orders", {
            "after": after.strftime("%Y-%m-%dT%H:%M:%S"),
            "dates_are_gmt": "true",
            "status": statuses,
            "per_page": PER_PAGE,
            "page": page,
            "orderby": "date",
            "order": "asc",
            "_fields": ORDER_FIELDS,
        })
        if not isinstance(batch, list):
            raise WooError("api", "Unexpected orders response from the store")
        orders.extend(batch)
        total_pages = int(number(headers.get("X-WP-TotalPages")))
        if page >= total_pages or not batch:
            return orders
        page += 1
        if page > MAX_PAGES:
            raise WooError("api", f"More than {MAX_PAGES * PER_PAGE} orders in this period")


def local_midnight(date, tz):
    return dt.datetime(date.year, date.month, date.day, tzinfo=tz)


def settings_hash(config, options):
    material = [config["url"], options["statuses"], options["assumedMargin"], options["costMetaKeys"]]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


def refresh(client, cache, options, signature, now, force):
    """Fetch only what is due: today always, recent days hourly, the rest daily."""
    now_ts = now.timestamp()
    store = cache.get("store")
    tz = store_timezone(store) if store else dt.timezone.utc
    today = now.astimezone(tz).date()
    covered_from = cache.get("coveredFrom")

    deep = (
        not store
        or cache.get("hash") != signature
        or not covered_from
        or needed_start(options, today).isoformat() < covered_from
        or now_ts - cache.get("deepAt", 0) >= DEEP_REFRESH_SECONDS
    )
    recent = (
        deep
        or force
        or cache.get("historyDate") != today.isoformat()
        or now_ts - cache.get("historyAt", 0) >= options["historyMinutes"] * 60
    )

    if recent:
        store = fetch_store(client)
        tz = store_timezone(store)
        today = now.astimezone(tz).date()

    needed = needed_start(options, today)
    if deep:
        start_date = needed
    elif recent:
        start_date = max(needed, today - dt.timedelta(days=RECENT_DAYS))
    else:
        start_date = today

    orders = fetch_orders(client, local_midnight(start_date, tz), options["statuses"])
    costs = load_product_costs(client, orders, options, cache.get("productCosts"), now_ts)
    fetched = aggregate(orders, tz, store["currency"], options, costs)

    start_key = start_date.isoformat()
    days = {} if deep else {day: v for day, v in cache.get("days", {}).items() if day < start_key}
    days.update(fetched)

    if recent:
        cache.update(historyAt=now_ts, historyDate=today.isoformat())
    if deep:
        cache.update(deepAt=now_ts, coveredFrom=start_key)
    cache.update(version=CACHE_VERSION, hash=signature, store=store, days=days, productCosts=costs,
                 todayAt=now_ts, attemptAt=now_ts, lastError=None)


def load_cache(profile):
    try:
        with open(cache_path(profile)) as handle:
            cache = json.load(handle)
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) and cache.get("version") == CACHE_VERSION else {}


def run_status(profile, options, force=False, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        options = {**load_prefs(profile), **options}
        if options.get("barPeriod") not in PERIOD_LABELS:
            options["barPeriod"] = "today"
        config = load_config(profile)
        os.makedirs(cache_dir(), mode=0o700, exist_ok=True)
        # Each monitor runs its own widget. The lock lets the first poll do the
        # work while the others wait, then read its result from the cache.
        with open(cache_path(profile) + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cache = load_cache(profile)
            signature = settings_hash(config, options)
            current = cache.get("hash") == signature
            recent = now.timestamp() - cache.get("attemptAt", 0) < options["maxAge"]
            today = now.astimezone(store_timezone(cache["store"])).date() if cache.get("store") else None
            # A newly selected long period can't wait for the next poll.
            uncovered = bool(today and cache.get("coveredFrom")
                             and needed_start(options, today).isoformat() < cache["coveredFrom"])

            if not force and current and recent and not uncovered:
                error = cache.get("lastError")
                if cache.get("store") and cache.get("days") is not None:
                    return snapshot(cache, profile, options, now, error["message"] if error else None)
                if error:
                    return error_output(WooError(error["kind"], error["message"]))

            client = Client(config, cache.get("apiRoot"))
            try:
                refresh(client, cache, options, signature, now, force)
                cache["apiRoot"] = client.api_root
            except WooError as error:
                if not current:
                    # These figures were built with other settings. Keeping them
                    # under the new hash would mix them with later fetches.
                    cache.pop("store", None)
                    cache.pop("days", None)
                cache.update(version=CACHE_VERSION, hash=signature, attemptAt=now.timestamp(),
                             lastError={"kind": error.kind, "message": error.message})
                write_private_json(cache_path(profile), cache)
                if cache.get("store"):
                    return snapshot(cache, profile, options, now, error.message)
                return error_output(error)
            write_private_json(cache_path(profile), cache)
            return snapshot(cache, profile, options, now)
    except WooError as error:
        return error_output(error)
    except Exception as error:
        return error_output(WooError("internal", f"Unexpected {type(error).__name__} while reading stats"))


# --- Commands ----------------------------------------------------------------

def parse_options(args):
    keys = [k.strip() for k in str(args.cost_meta_keys or "").split(",") if k.strip()]
    statuses = ",".join(s.strip() for s in str(args.statuses or "").split(",") if s.strip())
    return {
        "statuses": statuses or "processing,completed",
        "assumedMargin": max(0.0, min(100.0, number(args.assumed_margin))),
        "costMetaKeys": keys,
        "barFormat": args.bar_format or DEFAULT_BAR_FORMAT,
        "barPeriod": args.bar_period,
        "maxAge": max(0, args.max_age),
        "historyMinutes": max(1, args.history_minutes),
    }


def cmd_status(args):
    print(json.dumps(run_status(args.profile, parse_options(args), force=args.force), ensure_ascii=False))
    return 0


def cmd_prefs(args):
    try:
        changes = json.loads(args.set or "{}")
    except ValueError as exc:
        raise WooError("config", "--set needs valid JSON") from exc
    print(json.dumps(save_prefs(args.profile, changes)))
    return 0


def keyring_available():
    if not shutil.which("secret-tool"):
        return False
    try:
        probe = subprocess.run(
            ["secret-tool", "search", "application", APP],
            capture_output=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False
    return probe.returncode == 0


def cmd_setup(args):
    profile = checked_profile(args.profile)
    print("WooCommerce bar widget setup\n")
    url = normalize_url(input("Store URL (for example https://shop.example.com): "))
    print("\nCreate a read-only API key at:")
    print(f"  {url}/wp-admin/admin.php?page=wc-settings&tab=advanced&section=keys")
    print("Set Permissions to Read. This widget never needs to write to your store.\n")
    key = getpass.getpass("Consumer key (hidden): ").strip()
    secret = getpass.getpass("Consumer secret (hidden): ").strip()
    if not key or not secret:
        raise WooError("config", "Both the consumer key and the consumer secret are needed")

    print("\nChecking the key...")
    client = Client({"url": url, "key": key, "secret": secret})
    store = fetch_store(client)
    client.get("wc/v3/orders", {"per_page": 1, "_fields": "id"})

    config = {"url": url}
    saved_to_keyring = False
    if keyring_available():
        try:
            secret_store(profile, "consumer_key", key)
            secret_store(profile, "consumer_secret", secret)
            saved_to_keyring = True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print("Could not write to the keyring, so using a private file instead.")
    if saved_to_keyring:
        config["credentialStore"] = "keyring"
        where = "your keyring"
    else:
        config.update(credentialStore="file", consumerKey=key, consumerSecret=secret)
        where = f"{config_path(profile)} (only you can read it)"
    write_private_json(config_path(profile), config)
    try:
        os.unlink(cache_path(profile))
    except FileNotFoundError:
        pass

    print(f"Connected to {html.unescape(str(store.get('name') or url))}. API key saved in {where}.")
    print(f"Amounts will look like this: {money(1234.5, store_format(store))}")
    notice = timezone_notice(store)
    if notice:
        print(notice + ".")
    print()
    return run_doctor(profile, client, parse_options(args))


COST_KEY_HINT = re.compile(r"cost|cog|purchase|landed|wholesale|supplier", re.IGNORECASE)


def cost_report(orders, options, costs):
    """Summarize cost coverage. Only key names are collected, never their values."""
    sources = {}
    lines = covered = with_order_cost = 0
    candidates = set()
    for order in orders:
        order_level = order_cost(order) > 0
        with_order_cost += order_level
        candidates.update(meta_map(order.get("meta_data")))
        for line in order.get("line_items") or []:
            lines += 1
            value, source = line_cost(line, options, {} if order_level else costs)
            sources[source] = sources.get(source, 0) + 1
            covered += value > 0 or order_level
            candidates.update(meta_map(line.get("meta_data")))
    known = set(options["costMetaKeys"]) | set(ORDER_COST_KEYS + LINE_TOTAL_COST_KEYS + LINE_UNIT_COST_KEYS)
    if not orders:
        verdict = "no_orders"
    elif covered == 0:
        verdict = "none"
    elif covered < lines:
        verdict = "partial"
    else:
        verdict = "full"
    return {
        "orders": len(orders),
        "lines": lines,
        "covered": covered,
        "sources": sources,
        "orderLevel": with_order_cost,
        "verdict": verdict,
        "candidates": sorted(key for key in candidates - known if COST_KEY_HINT.search(str(key))),
    }


def print_cost_report(report):
    labels = {
        "core": "WooCommerce core cost of goods, on the order line",
        "line-meta": "cost plugin data, on the order line",
        "product": "the product's current cost, multiplied by quantity",
        "none": "no cost on the line",
    }
    print(f"Looked at {report['orders']} orders with {report['lines']} lines.")
    for source, label in labels.items():
        if report["sources"].get(source):
            print(f"  {report['sources'][source]:>4} lines: {label}")
    if report["orderLevel"]:
        print(f"  {report['orderLevel']:>4} orders have a total cost on the order")
    print()

    verdict = report["verdict"]
    if verdict == "no_orders":
        print("Profit: can't be checked yet, because there are no orders.")
        return
    if verdict == "full":
        print("Profit: ready. Every line in these orders has a cost.")
    elif verdict == "partial":
        share = report["covered"] / report["lines"] * 100
        print(f"Profit: partial. {share:.0f}% of these lines have a cost, so profit is marked ~ "
              "and only counts those sales.")
    else:
        print("Profit: not available yet. No cost data was found, so profit will show as —.")
    if report["sources"].get("product") and verdict != "none":
        print("Costs taken from products use today's cost, so older orders may not reflect what you paid then.")
    if verdict == "full":
        return

    print("\nTo add cost data, use one of these:")
    print("  - Turn on Cost of Goods Sold under WooCommerce > Settings > Advanced > Features")
    print("  - Set the widget's costMetaKeys to the per-unit cost key your cost plugin uses")
    print("  - Set the widget's assumedMarginPercent for an estimate, marked ~")
    if report["candidates"]:
        print("\nThese keys on your orders might hold costs:")
        for key in report["candidates"]:
            print(f"  {key}")
    print("\nThe README explains how to change widget settings.")


def run_doctor(profile, client, options):
    print("Checking the latest orders for cost data...")
    orders, _ = client.get("wc/v3/orders", {
        "per_page": 50, "status": options["statuses"], "orderby": "date", "order": "desc", "_fields": ORDER_FIELDS,
    })
    costs = load_product_costs(client, orders, options, {}, time.time())
    print_cost_report(cost_report(orders, options, costs))
    return 0


def cmd_doctor(args):
    config = load_config(args.profile)
    client = Client(config)
    fetch_store(client)
    return run_doctor(args.profile, client, parse_options(args))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    handlers = {"status": cmd_status, "setup": cmd_setup, "doctor": cmd_doctor, "prefs": cmd_prefs}
    for name, handler in handlers.items():
        command = sub.add_parser(name)
        command.set_defaults(handler=handler)
        command.add_argument("--profile", default="default")
        command.add_argument("--statuses", default="processing,completed")
        command.add_argument("--assumed-margin", type=float, default=0)
        command.add_argument("--cost-meta-keys", default="")
        command.add_argument("--bar-format", default=DEFAULT_BAR_FORMAT)
        command.add_argument("--bar-period", default="today")
        command.add_argument("--max-age", type=int, default=270)
        command.add_argument("--history-minutes", type=int, default=60)
        command.add_argument("--force", action="store_true")
        command.add_argument("--set", default="")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except WooError as error:
        print(f"Error: {error.message}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
