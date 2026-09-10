# Crypto Trading System

A real-time crypto trading system that consumes public Binance Spot market data,
builds one-minute OHLC candles, runs two SMA/EMA strategy variants, places sample
orders on Binance Spot Demo Mode, and exposes the system state to a JavaScript frontend.

## Target architecture

```text
Binance Spot public WebSocket
          |
          v
    Market data service ---> Tick store
          |
          v
   One-minute aggregator ---> Candle store ---> Strategy engine
                                      |               |
                                      v               v
                               REST/WebSocket API   Order service
                                      |               |
                                      v               v
                               JS frontend       Binance Demo REST API
                                                      |
                                                      v
                                                   Trade log
```

## Project structure

```text
backend/
|-- main.py                    # Backend entry point; connects all modules
|-- config.py                  # YAML/environment configuration loader
|-- requirements.txt           # Python dependencies
|-- api/
|   |-- routes.py              # REST API endpoints
|   `-- __init__.py
|-- models/
|   `-- __init__.py            # Tick, candle, signal, position, and trade models
`-- services/
    |-- market_data.py         # Binance WebSocket connection and tick handling
    |-- candle_service.py      # One-minute OHLC aggregation
    |-- strategy_service.py    # SMA/EMA calculations and signals
    |-- order_service.py       # Binance Demo orders, risk, P&L, and order lists
    |-- pretrade_risk.py       # Atomic per-account capital reservations
    |-- reconciliation_service.py # Unknown-order reconciliation by client ID
    |-- state_repository.py    # SQLite order/position persistence
    `-- __init__.py

frontend/
|-- index.html                 # Dashboard page
|-- app.js                     # Backend API/WebSocket client
`-- styles.css                 # Dashboard styling

config.yaml                    # Runtime values and safe defaults
.env.example                   # Secret environment-variable template
```

## Step-by-step roadmap

### Phase 1: Foundation and configuration

- [x] Create separate `backend` and `frontend` folders.
- [x] Add `backend/main.py` as the backend entry point.
- [x] Add placeholder REST endpoints and a frontend API request.
- [x] Create and document a Python virtual environment.
- [x] Add all pinned backend dependencies to `requirements.txt`.
- [x] Add a `.env.example` containing variable names but no secrets.
- [x] Load Binance credentials and runtime settings from environment variables.
- [x] Add structured logging with timestamps and log levels.
- [x] Define development and production-safe configuration defaults.

Runtime values are stored in `config.yaml`. Secret fields reference environment
variables so credentials are never committed:

```yaml
binance_api_key: ${BINANCE_API_KEY:-}
binance_api_secret: ${BINANCE_API_SECRET:-}
trading_symbols: BTCUSDT,ETHUSDT
order_execution_enabled: false
```

#### Local backend setup

Python 3.11 or newer is recommended. From the repository root on PowerShell:

```powershell
python -m venv .venv
# Standard Windows Python:
.\.venv\Scripts\Activate.ps1
# If your Python distribution creates a bin directory instead:
# .\.venv\bin\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt
Copy-Item .env.example .env
python -m uvicorn main:app --app-dir backend --reload
```

The API is then available at `http://127.0.0.1:8000`. In development, its
interactive documentation is available at `http://127.0.0.1:8000/docs`.

#### One-command startup

From PowerShell in the repository root, run:

```powershell
.\start.ps1
```

The launcher creates `.venv` when necessary, installs the pinned dependencies,
creates a local `.env` from `.env.example` when missing, starts the backend, and
opens the API-served JavaScript dashboard. The backend and frontend share one
process, so pressing `Ctrl+C` stops the complete application.

Credentials are collected during account creation and may be replaced from the
dashboard after password confirmation. They are encrypted in SQLite, verified
against Binance Spot Testnet, and decrypted only into the signed-in user's
backend session. `.env` remains supported for unattended local testing and is
ignored by Git.

#### Configuration behavior

- `config.yaml` is the source for all runtime values and defaults.
- Any YAML field can be overridden with an uppercase environment variable using
  the same name, such as `APP_PORT` or `LOG_LEVEL`.
- `CONFIG_FILE` can select a different YAML file for deployment or testing.
- Development is the default environment and exposes API documentation.
- Production disables `/docs` and `/redoc` and should use an explicit
  `CORS_ORIGINS` allowlist.
- Order execution is disabled by default in every environment.
- Binance API credentials are optional during read-only development and are
  represented as secret values so accidental object rendering masks them.
- Invalid ports, percentages, log levels, or empty symbol lists fail fast during
  application startup.
- Logs are emitted as one JSON object per line with UTC timestamps.

### Phase 2: Shared data models

- [x] Define a `Tick` model with symbol, price, quantity, and UTC timestamp.
- [x] Define a `Candle` model with symbol, interval start, interval end, open,
      high, low, close, tick count, and optional volume.
- [x] Define a `Signal` model with symbol, action, timestamp, and strategy values.
- [x] Define a `Position` model with variant, entry price, size, current P&L,
      stop-loss price, take-profit price, and status.
- [x] Define a `Trade` model with timestamp, symbol, side, quantity, execution
      price, Binance order ID, strategy variant, and reason.
- [x] Use UTC-aware timestamps and `Decimal` for monetary values.

### Phase 3: Binance market-data ingestion

- [x] Connect to Binance Spot Testnet WebSocket for live prices.
- [x] Subscribe to BTCUSDT and ETHUSDT initially.
- [x] Normalize Binance symbols consistently throughout the application.
- [x] Parse and validate every incoming tick.
- [x] Maintain the latest tick per symbol in a concurrency-safe in-memory store.
- [x] Support adding and removing active symbols without restarting the app.
- [x] Add automatic reconnection with exponential backoff.
- [x] Detect stale connections and restart subscriptions when necessary.
- [x] Handle malformed messages without stopping the stream.
- [x] Shut down sockets and background tasks cleanly.

Implementation notes:

- The client uses Binance Spot's public raw stream endpoint and dynamically
  sends `SUBSCRIBE` and `UNSUBSCRIBE` messages for `<symbol>@ticker`.
- The displayed price is Binance's official last-trade price field (`c`), with
  its corresponding last quantity (`Q`) and exchange event timestamp (`E`).
- Testnet public market data requires no API credentials. Signed order
  placement is restricted to the configured official Spot Testnet host.
- Binance symbols are lowercased only in stream names; internal symbols remain
  normalized uppercase values.
- The WebSocket library handles protocol ping/pong frames, while an application
  stale timeout forces reconnection when no messages arrive.
- Control messages are rate-limited, and the configured subscription count is
  capped at Binance's documented maximum of 1,024 streams per connection.
- Connections are expected to be recycled by Binance after 24 hours, so every
  disconnect re-enters the bounded exponential-backoff loop and resubscribes.
- Invalid JSON, unrelated event types, malformed trades, and downstream tick
  handler failures are logged without terminating ingestion.

### Phase 4: One-minute OHLC candle aggregation

- [x] Bucket ticks by exact UTC minute boundaries.
- [x] Set the first tick as open and update high, low, and close correctly.
- [x] Track tick count. Volume remains unset because ticker updates are not
      equivalent to executed trade volume.
- [x] Finalize each candle exactly when its minute closes.
- [x] Prevent overlapping or duplicate candles.
- [x] Skip empty minutes rather than fabricating candles without market data.
- [x] Ignore ticks older than the current candle to protect finalized history.
- [x] Store the current candle and a bounded rolling history per symbol.
- [x] Notify API/WebSocket consumers whenever a candle changes or finalizes.

The candle service uses O(1) dictionary access for the active candle and a
bounded `deque` for finalized history. Each tick performs a constant amount of
OHLC work. The minute-boundary sweep is O(s), where s is the number of active
symbols and every active candle must be inspected once.

### Phase 5: REST and custom WebSocket API

- [x] Add a health endpoint showing service and Binance connection status.
- [x] Add an endpoint listing active symbols.
- [x] Add endpoints to add and remove symbols safely.
- [x] Add an endpoint returning the latest tick for each symbol.
- [x] Add an endpoint returning current and historical candles.
- [x] Add endpoints returning signals, positions, and trade history.
- [x] Replace the placeholder dashboard response with real application state.
- [x] Add live candle messages to the custom `/ws/ticks` WebSocket endpoint.
- [x] Remove disconnected WebSocket clients without interrupting ingestion.
- [x] Add request validation, useful HTTP errors, and interactive API documentation.

Proposed endpoints:

```text
GET    /api/health
GET    /api/symbols
POST   /api/symbols
DELETE /api/symbols/{symbol}
GET    /api/ticks/latest
GET    /api/candles/{symbol}
GET    /api/chart-candles/{symbol}?interval={interval}
GET    /api/order-book/{symbol}
GET    /api/indicators/{symbol}
GET    /api/signals
GET    /api/positions
GET    /api/orders
POST   /api/orders?test={true|false}
GET    /api/orders/open
GET    /api/orders/status
DELETE /api/orders
DELETE /api/orders/open/{symbol}
GET    /api/account
GET    /api/pnl/{symbol}
POST   /api/orders/{symbol}/{order_id}/square-off
GET    /api/order-lists
POST   /api/order-lists
GET    /api/order-lists/status
DELETE /api/order-lists
GET    /api/dashboard
WS     /ws/ticks
```

The implemented WebSocket path is `/ws/ticks`; it multiplexes tick batches,
candle batches, and on-demand partial order-book snapshots over one connection.
The browser sends `subscribe_depth` and `unsubscribe_depth` actions when a card
is opened or closed.

### Phase 6: SMA/EMA strategy

- [x] Use SMA crossing EMA as the exact entry and exit rule.
- [x] Make the fast and slow lookback periods configurable.
- [x] Calculate strategy indicators only from finalized candles.
- [x] Emit a BUY signal when fast SMA crosses above slow EMA.
- [x] Emit an EXIT signal when fast SMA crosses below slow EMA.
- [x] Emit only on relation changes rather than repeating every candle.
- [x] Wait for the configured slow-period history before evaluating signals.
- [x] Run identical signal logic for both risk variants.
- [x] Store bounded signal and indicator histories with their input values.

Initial suggested rule:

```text
BUY:  fast SMA crosses above slow EMA
EXIT: fast SMA crosses below slow EMA, or SL/TP is triggered
```

The strategy keeps a rolling close sum, EMA accumulator, sample count, and
previous SMA/EMA relation per symbol. Each finalized-candle update is expected
O(1). Historical indicator and signal responses are O(requested records).

The chart toolbar includes `1m`, `5m`, `15m`, `1H`, `4H`, and `1D` Binance
candle views, a color-coded volume histogram, and an `SMA/EMA` toggle. Enabling it on the one-minute view loads
the selected symbol's SMA/EMA history from `/api/indicators/{symbol}`
and displays yellow SMA and purple EMA lines plus the current values and latest
A/B crossover signal. Switching symbol tabs reloads the corresponding indicator
series. The strategy indicators remain restricted to `1m` because the strategy
evaluates finalized one-minute candles; other chart intervals disable the toggle
to avoid presenting misleading values. The visual toggle does not stop strategy
evaluation.

### Phase 7: Position and risk management

- [x] Maintain independent positions for Variant A and Variant B.
- [x] Record entry price, current price, quantity, and unrealized P&L.
- [x] Calculate stop-loss and take-profit levels at entry time.
- [x] Exit a position once its SL or TP condition is met.
- [x] Prevent duplicate and concurrent positions per symbol/variant.
- [x] Open/close local state only for Binance-reported executed quantity.
- [x] Provide signed query/open-order APIs for reconciliation.
- [x] Keep the global order-placement switch disabled by default.

Important assignment clarification: it labels a 15% stop loss as tighter and a
10% stop loss as looser. Normally, 10% is tighter because the exit is closer to
the entry price. Until clarified, this roadmap uses Variant A = 10% and Variant
B = 15%, while keeping both values configurable.

### Phase 8: Binance Testnet order execution

- [x] Implement HMAC-SHA256 authenticated Binance Spot Testnet REST requests.
- [x] Allow only official Spot Testnet or Demo Mode order hosts.
- [x] Fetch and cache symbol filters such as minimum quantity, step size, and tick size.
- [x] Normalize configured quantities and prices to exchange filters.
- [x] Support all seven current single Spot order types and automated MARKET signals.
- [x] Add request timeouts and avoid unsafe order retries.
- [x] Use deterministic strategy client order IDs to prevent
      duplicate orders.
- [x] Record order responses and lifecycle updates in bounded memory.
- [x] Never log API secrets, signatures, or signed request URLs.

Supported single-order types are `MARKET`, `LIMIT`, `STOP_LOSS`,
`STOP_LOSS_LIMIT`, `TAKE_PROFIT`, `TAKE_PROFIT_LIMIT`, and `LIMIT_MAKER`.
Their type-specific required parameters, time-in-force values, stop/trailing
triggers, iceberg constraints, pegging fields, and symbol filters are validated
before signing. Binance order-list workflows (OCO, OPO/OPOCO, and OTO/OTOCO)
use their distinct official endpoints through `/api/order-lists`; they are not
misrepresented as single-order types.

### Phase 9: Trade logging and persistence

- [x] Maintain an in-memory trade log for API access.
- [x] Persist trades and strategy positions to SQLite.
- [x] Store timestamp, symbol, side, size, price, strategy variant, order ID,
      status, and exit reason.
- [x] Restore local state after restart and reconcile account orders/fills with Binance.
- [x] Add filtering by symbol, variant, side, status, and time range.

### Phase 10: JavaScript frontend

- [x] Build a clear dashboard layout.
- [x] Show backend and Binance connection status.
- [x] Show active symbols and controls for adding/removing them.
- [x] Display the latest price and timestamp for each symbol.
- [x] Display recent one-minute OHLC candles.
- [x] Add a TradingView Lightweight Charts candlestick chart that loads bounded
      REST history once and applies live WebSocket candles with O(1) `update`.
- [x] Connect to the backend WebSocket for live tick, candle, indicator, signal,
      and on-demand depth updates.
- [x] Automatically reconnect the frontend WebSocket after disconnection.
- [x] Display current strategy signals and indicator values.
- [x] Display Variant A and Variant B positions and P&L separately.
- [x] Display Binance order history, execution status, FIFO P&L, and square-off controls.
- [x] Show loading, empty, disconnected, and error states.
- [x] Remove the hard-coded backend host by using same-origin API URLs and
      browser-safe configuration from `/api/config`.

### Current dashboard

Start the backend and open `http://127.0.0.1:8000/dashboard/`. The backend
serves the JavaScript application, which obtains all data through REST and
WebSocket APIs. Binance credentials remain exclusively on the backend and are
never included in browser responses.

Current complexity characteristics:

- Latest tick update and single-symbol lookup: expected O(1) dictionary access.
- Symbol membership, addition, and removal: expected O(1) set operations.
- WebSocket client registration and removal: expected O(1) set operations.
- Listing all symbols or ticks: O(n), because every requested item must be
  serialized and returned.
- Broadcasting a tick: O(c), where c is the number of connected browser clients.
- High-frequency updates are coalesced by symbol every configured 100 ms, and
  the browser updates only changed cards on its next animation frame instead of
  rebuilding the complete price grid.

Clicking a symbol card opens a responsive trading workspace with a selectable
timeframe candlestick chart on the left and its on-demand 10-level partial order book on
the right. The initial 120 finalized candles are seeded from Binance's public
`/api/v3/klines` endpoint and served through our REST API. Subsequent current
candle updates come from our own live aggregation pipeline. The chart uses
TradingView Lightweight Charts 5.2.0 and includes the required TradingView
attribution and link.

While the trading workspace is open, the dashboard summary and roadmap are
hidden to maximize chart space. Active symbols appear as tabs along the top;
selecting another tab replaces the chart, OHLC values, and on-demand depth
subscription without leaving the workspace. Bid and ask depth tables are shown
side by side in the right panel.

Hovering a depth level reveals Buy and Sell actions that prefill the Spot order
ticket with that exact limit price. The ticket supports Market, Limit,
Conditional, and Post-only entry modes. Its management panel lists open Binance
orders with cancellation controls and independent strategy positions with a
manual square-off action. Keyboard shortcuts are `B`/`S` for side, `L`/`M` for
order type, `Ctrl+Enter` to submit, and `Esc` to reset; shortcuts never bypass
the backend kill switch or exchange validation.

Spot P&L is reconstructed from Binance's signed `myTrades` fill ledger using
FIFO lots. Quote/base commissions are included directly, realized P&L is
assigned to sell orders, and unrealized P&L is assigned to remaining buy lots
at the current live price. Commissions paid in a third asset are disclosed
separately rather than converted using an inaccurate present-day rate. Clicking
Square off submits an immediate idempotent MARKET sell for that buy lot's
remaining quantity.
After the FIFO basis is loaded, unrealized totals and per-order values are
recomputed on every selected-symbol Binance ticker update without polling the
account API. The order ticket can be closed independently and reopened with the
`Trade` button or any trading keyboard shortcut.

If no active trading session exists, the first submit opens an inline Binance
Demo credential prompt. The backend verifies the credentials with the signed
account endpoint, retains them only as masked secret objects in process memory,
and resumes the pending order after a successful connection. The browser fields
are cleared immediately; credentials are never written to YAML, `.env`, browser
storage, logs, or the order audit. Stopping the backend clears the session.
The first order fetches only that symbol's exchange filters (cached afterward)
rather than Binance's complete symbol catalogue. Backend and browser deadlines
ensure a slow upstream request returns an actionable timeout instead of leaving
the ticket indefinitely in a submitting state.

```text
GET    /api/order-session
POST   /api/order-session
DELETE /api/order-session
```

Binance depth snapshots continue arriving at up to 100 ms intervals. To prevent
visual flicker, the browser keeps only the newest pending snapshot in O(1) and
renders the depth tables at the configurable
`order_book_render_interval_milliseconds` threshold (500 ms by default). This
is a trailing-edge throttle: intermediate frames are skipped, but each rendered
frame uses the latest data available at that moment.

### Phase 11: Testing

- [x] Unit-test tick parsing and UTC timestamp normalization.
- [x] Unit-test OHLC calculations and exact minute boundaries.
- [x] Test late ticks, duplicate ticks, and symbol separation; empty minutes are intentionally skipped.
- [x] Unit-test SMA/EMA calculations and crossover signals.
- [x] Unit-test FIFO P&L, commissions, kill switch, and duplicate prevention paths.
- [x] Mock Binance REST responses for deterministic order and P&L tests.
- [x] Exercise reconnect/stale handling through bounded state-machine logic and manual disconnect tests.
- [x] Test API payload validation and failure guards.
- [x] Validate frontend syntax, timeout recovery, and WebSocket error branches.
- [x] Run controlled end-to-end Demo Mode MARKET orders and verify Binance fills.

### Phase 12: Documentation and delivery

- [x] Document prerequisites and exact setup commands.
- [x] Document how to create and use Binance Spot Testnet credentials safely.
- [x] Document configuration fields without exposing credentials.
- [x] Document the strategy, SL/TP calculations, P&L, and assumptions.
- [x] Document how to start the backend and frontend.
- [x] Provide example API requests and WebSocket messages.
- [x] Document known limitations and future improvements.
- [x] Restrict market data and signed order execution to official Binance
      Spot Testnet endpoints by default.

## Testnet credentials

Create a new key from Binance Spot Testnet's API Key Management page. Never use
a production key and never paste a secret into source files. The recommended
flow is to leave `.env` blank, prepare an order in the dashboard, and enter the
Testnet key during account creation. Credentials are encrypted at rest and can
only be replaced after confirming the account password. Revoke any key disclosed
in chat, screenshots, logs, or source control immediately.

## API examples

Create a validation-only market order (the request is signed but not matched):

```powershell
$body = @{ symbol = "BTCUSDT"; side = "BUY"; type = "MARKET"; quoteOrderQty = "10" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/orders?test=true" -ContentType "application/json" -Body $body
```

Filter order history and inspect FIFO P&L:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/orders?symbol=BTCUSDT&side=BUY&status=FILLED"
Invoke-RestMethod "http://127.0.0.1:8000/api/pnl/BTCUSDT"
```

WebSocket clients connect to `ws://127.0.0.1:8000/ws/ticks` and can request
depth with `{"action":"subscribe_depth","symbol":"BTCUSDT"}` or release it
with the corresponding `unsubscribe_depth` action.

Run the complete offline test suite:

```powershell
$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Known limitations

- This is a single-process educational Demo Mode system, not production trading
  infrastructure or financial advice.
- User credentials persist encrypted in SQLite; decrypted copies exist only in
  active backend sessions.
- Third-asset commissions are reported without historical USDT conversion.
- SQLite stores audit/strategy state locally; Binance remains authoritative for
  balances, fills, open orders, and order lists.
- The chart library loads from a public CDN and therefore needs internet access.

## Suggested implementation order

Complete the phases in this order so each layer can be tested before the next
one depends on it:

1. Configuration and models
2. Market-data ingestion
3. Candle aggregation
4. Read-only REST and WebSocket APIs
5. Strategy calculations
6. Position and risk management
7. Testnet order execution
8. Trade persistence
9. Frontend dashboard
10. Automated and end-to-end testing
11. Documentation and final review

## Definition of done

The project is complete when it can run continuously, stream at least BTCUSDT
and ETHUSDT from Binance Testnet, produce correct one-minute candles, broadcast
updates to the JavaScript frontend, execute the two configured strategy
variants independently, place only small Testnet orders, recover gracefully
from client and Binance disconnections, and provide a complete auditable trade
log with automated tests for the critical calculations.

## Safety rules

- Use Binance Testnet only.
- Never commit `.env` files or API credentials.
- Keep order sizes small and configurable.
- Validate every symbol and order against Binance exchange filters.
- Provide a kill switch before enabling automated order placement.
- Treat REST/WebSocket data as untrusted input and validate it.

# Continuous integration and delivery

The GitHub Actions workflow in `.github/workflows/ci-cd.yml` runs the Python
test suite, compiles the backend, validates the frontend JavaScript, imports the
application with external market streaming disabled, and smoke-tests a complete
Docker image for every pull request and push to `main`.

After a successful `main` build, it publishes these images to GitHub Container
Registry:

- `ghcr.io/prathamdesai07/cryptotradingsystem:latest`
- `ghcr.io/prathamdesai07/cryptotradingsystem:sha-<commit>`

Run the same production image locally with persistent trading state:

```powershell
docker build -t crypto-trading-system .
docker run --rm -p 8000:8000 -v crypto-trading-data:/app/data crypto-trading-system
```

Open `http://localhost:8000/dashboard/`. Credentials are not built into the
image; enter Demo Mode credentials at runtime or inject them through protected
deployment secrets. Automatic order execution remains disabled unless a valid
runtime session is connected or the explicit execution setting is enabled.
