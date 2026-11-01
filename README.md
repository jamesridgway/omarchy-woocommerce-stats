# Store Stats for WooCommerce

Shows revenue, profit, margin and order counts from any WooCommerce store in
the Omarchy bar. It uses a read-only API key and needs nothing installed on the
store. Python 3 is the only requirement, and Omarchy already includes it. If
`secret-tool` is installed, the API key is kept in your keyring.

```
󰄐 £1,284 · 17 · 34%
```

Click for a daily trend chart and a table of the periods you choose.

| Input | Action |
|---|---|
| Left click | Open the panel |
| Middle click, or `r` in the panel | Refresh now |
| Right click, or `o` in the panel | Open WooCommerce analytics |
| Gear icon, or `s` in the panel | Choose breakdowns, trend length and week start |
| `m` in the panel | Switch the chart between revenue, profit and orders |

This is an independent project, not made or endorsed by WooCommerce or Automattic.
WooCommerce and its associated designs are trademarks of Automattic Inc.

## Install

```bash
omarchy plugin add https://github.com/jamesridgway/omarchy-woocommerce-stats.git --enable
```

For development, run `./install.sh` from a checkout instead.

## Remove

```bash
omarchy plugin remove jamesridgway.woocommerce
```

That takes the widget off your bar and leaves your store connection in place, so
reinstalling doesn't need setup again. To delete that too, including the API key:

```bash
secret-tool clear application omarchy-woocommerce
rm -rf ~/.config/omarchy-woocommerce ~/.cache/omarchy-woocommerce
```

You can also revoke the key under WooCommerce > Settings > Advanced > REST API.

## Store requirements

- WooCommerce with its REST API reachable over HTTPS
- A permalink setting other than Plain. WooCommerce 11.1 and later also accept
  API keys on Plain, and the widget uses that if it's all a store offers

Custom REST prefixes are found automatically. If a security plugin hides the
store's public details, the store name falls back to its address and days use the
UTC offset of the latest order, and the panel tells you.

## Connect a store

Click the widget and choose **Set up store**, or run:

```bash
python3 ~/.config/omarchy/plugins/jamesridgway.woocommerce/woo_stats.py setup
```

Setup asks for the store URL and an API key, which you create under
WooCommerce > Settings > Advanced > REST API with **Read** permission. The key
goes in your keyring, or in a private file if no keyring is available. It is
never stored in `shell.json`, so that file stays safe to keep in dotfiles.

## What the numbers mean

| Field | Meaning |
|---|---|
| `{revenue}` | Order totals minus tax and refunds. Includes shipping charged |
| `{gross}` | What customers paid, minus refunds. Includes tax and shipping |
| `{profit}` | Gross profit: product sales after discounts, minus cost of goods |
| `{margin}` | Profit as a percentage of the product sales it covers |
| `{orders}` | Orders with a counted status, `processing` and `completed` by default |
| `{aov}` | Revenue per order |
| `{period}` | Name of the period shown |

Profit leaves out shipping, payment fees and overheads, so it isn't net profit.

Amounts follow the store's currency settings, including symbol position and
separators. Days follow the store's timezone. A refund counts against the day of the
original order, so WooCommerce Analytics, which uses the refund date, can show
different daily figures. Partial refunds scale tax and cost in proportion.

### Where costs come from

WooCommerce has no universal cost field. For each order line, the first of these
that has a value is used:

1. WooCommerce's core Cost of Goods Sold value on the order line
2. Cost plugin meta on the order line (WooCommerce Cost of Goods,
   Cost of Goods for WooCommerce, or your `costMetaKeys`)
3. An order-level total, when no line in the order has a cost
4. The product's current cost multiplied by quantity
5. Your `assumedMarginPercent`, if you set one

Support for the plugin meta keys is best effort. To see what your store provides,
and which order keys might hold costs if none are recognised:

```bash
python3 ~/.config/omarchy/plugins/jamesridgway.woocommerce/woo_stats.py doctor
```

Profit is never guessed. With no cost data it shows `—`. A `~` means some sales
have no known cost or use your assumed margin, and the panel explains which.

## Panel settings

Use the gear icon in the panel to choose:

- **Breakdowns:** today, yesterday, this week, last week, last 7, 30 or 90 days,
  this month, last month, this quarter, last quarter, this year and last year
- **Trend:** off, or the last 7, 14, 30 or 90 days
- **Week start:** Monday or Sunday

These are saved per store in `~/.config/omarchy-woocommerce/<profile>.prefs.json`,
not in `shell.json`, so two stores can't overwrite each other's choices. You can
also set them from a terminal:

```bash
python3 woo_stats.py prefs --set '{"periods": ["today", "mtd", "ytd"], "trendDays": 90}'
```

In the chart, today's bar is lighter because the day isn't over. Hover over a bar
to see that day's revenue, orders and profit. A day with orders but no cost data
leaves a gap in the profit chart rather than showing zero.

Today's orders are fetched on every refresh, the last 35 days every
`historyRefreshMin`, and the rest of a long range once a day. A late refund on an
older order can therefore take up to a day to appear.

## Widget settings

Omarchy has no settings screen for widgets at the moment, so change these with
`omarchy bar set jamesridgway.woocommerce <setting> <value>`. If you run more than one
store, edit `shell.json` directly, because that command may not tell two widgets
apart:

| Setting | Default | Purpose |
|---|---|---|
| `profile` | `default` | Saved store connection. Use one per store |
| `barFormat` | `󰄐 {revenue} · {orders} · {margin}` | Bar text. Use only an icon to keep figures private |
| `barPeriod` | `today` | Any breakdown: `today`, `yesterday`, `wtd`, `last_week`, `7d`, `30d`, `90d`, `mtd`, `last_month`, `qtd`, `last_quarter`, `ytd` or `last_year` |
| `refreshIntervalSec` | `300` | How often today's orders are fetched |
| `historyRefreshMin` | `60` | How often earlier days are re-fetched |
| `statuses` | `processing,completed` | Order statuses to count |
| `costMetaKeys` | empty | Extra per-unit cost meta keys |
| `assumedMarginPercent` | `0` | Margin assumed for products with no cost |

To monitor another store, add a second widget and connect it with
`woo_stats.py setup --profile <name>`.

Orders in a currency other than the store's are left out, and the panel says how
many.

## Privacy

The widget only requests the order fields it needs, which exclude customer
details. The cache in `~/.cache/omarchy-woocommerce/` holds daily totals and
product costs. It is readable only by you.

## Development

```bash
python3 -m unittest discover tests
omarchy plugin validate .
./install.sh
```

The shell's plugin hot reload doesn't always replace a loaded `Panel.qml`. If a
QML change doesn't appear after installing, run `omarchy restart shell`.
