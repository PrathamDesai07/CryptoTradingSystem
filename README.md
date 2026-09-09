# Crypto Trading System

A real-time crypto trading system that consumes public Binance Spot market data,
builds one-minute OHLC candles, runs two SMA/EMA strategy variants, places sample
orders on Binance Testnet, and exposes the system state to a JavaScript frontend.

> This repository currently contains only the initial project structure and
> placeholder API/frontend files. The features below are the implementation
> roadmap, not completed functionality.

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
                               JS frontend       Binance Testnet REST API
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
    |-- order_service.py       # Binance Testnet order placement
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

Before entering real Testnet credentials in `.env`, confirm that `.env` remains
ignored by Git. Keep `ORDER_EXECUTION_ENABLED=false` until the order service is
implemented and deliberately tested.

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

- [x] Connect to the Binance Testnet WebSocket market stream.
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
- Public production market data is read-only and requires no API credentials.
  All future order placement remains restricted to Binance Spot Testnet.
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

- [ ] Bucket ticks by exact UTC minute boundaries.
- [ ] Set the first tick as open and update high, low, and close correctly.
- [ ] Track tick count and, if available, traded volume.
- [ ] Finalize each candle exactly when its minute closes.
- [ ] Prevent overlapping or duplicate candles.
- [ ] Decide and document how minutes with no ticks are handled.
- [ ] Decide and document how late or out-of-order ticks are handled.
- [ ] Store the current candle and a bounded rolling history per symbol.
- [ ] Notify API/WebSocket consumers whenever a candle changes or finalizes.

### Phase 5: REST and custom WebSocket API

- [x] Add a health endpoint showing service and Binance connection status.
- [x] Add an endpoint listing active symbols.
- [x] Add endpoints to add and remove symbols safely.
- [x] Add an endpoint returning the latest tick for each symbol.
- [ ] Add an endpoint returning current and historical candles.
- [ ] Add endpoints returning signals, positions, and trade history.
- [x] Replace the placeholder dashboard response with real application state.
- [ ] Add a custom WebSocket endpoint for live candle updates. A live tick
      endpoint is available now at `/ws/ticks`; candle events belong to Phase 4.
- [x] Remove disconnected WebSocket clients without interrupting ingestion.
- [ ] Add request validation, useful HTTP errors, and basic API documentation.

Proposed endpoints:

```text
GET    /api/health
GET    /api/symbols
POST   /api/symbols
DELETE /api/symbols/{symbol}
GET    /api/ticks/latest
GET    /api/candles/{symbol}
GET    /api/signals
GET    /api/positions
GET    /api/trades
GET    /api/dashboard
WS     /ws/candles
```

### Phase 6: SMA/EMA strategy

- [ ] Choose and document the exact signal rule.
- [ ] Make the fast and slow lookback periods configurable.
- [ ] Calculate indicators only from finalized candles.
- [ ] Emit a BUY signal when the selected bullish crossover occurs.
- [ ] Emit a SELL/EXIT signal when the selected bearish crossover occurs.
- [ ] Avoid repeating the same signal on every candle.
- [ ] Wait for enough candle history before evaluating the strategy.
- [ ] Run the same entry/exit logic for both risk variants.
- [ ] Store strategy decisions with input values for later debugging.

Initial suggested rule:

```text
BUY:  fast SMA crosses above slow EMA
EXIT: fast SMA crosses below slow EMA, or SL/TP is triggered
```

### Phase 7: Position and risk management

- [ ] Maintain independent positions for Variant A and Variant B.
- [ ] Record entry price, current price, quantity, and unrealized P&L.
- [ ] Calculate stop-loss and take-profit levels at entry time.
- [ ] Exit a position once its SL or TP condition is met.
- [ ] Prevent duplicate positions unless explicitly permitted.
- [ ] Define behavior when an order is rejected or only partially filled.
- [ ] Reconcile local position state with Binance responses.
- [ ] Add a global switch that disables all order placement.

Important assignment clarification: it labels a 15% stop loss as tighter and a
10% stop loss as looser. Normally, 10% is tighter because the exit is closer to
the entry price. Until clarified, this roadmap uses Variant A = 10% and Variant
B = 15%, while keeping both values configurable.

### Phase 8: Binance Testnet order execution

- [ ] Implement authenticated Binance Testnet REST requests.
- [ ] Confirm that the configured URLs point only to Testnet.
- [ ] Fetch symbol filters such as minimum quantity, step size, and tick size.
- [ ] Convert the configured dummy order size into a valid quantity.
- [ ] Place small market or limit orders when valid signals occur.
- [ ] Add request timeouts and bounded retries where safe.
- [ ] Do not retry an uncertain order blindly; use client order IDs to prevent
      duplicate orders.
- [ ] Record request, response, rejection, and fill information.
- [ ] Never log API secrets or signed request credentials.

### Phase 9: Trade logging and persistence

- [ ] Maintain an in-memory trade log for API access.
- [ ] Persist trades to a local database or append-only file.
- [ ] Store timestamp, symbol, side, size, price, strategy variant, order ID,
      status, and exit reason.
- [ ] Restore essential state after a restart or reconcile it with Binance.
- [ ] Add filtering by symbol, variant, side, and time range.

### Phase 10: JavaScript frontend

- [x] Build a clear dashboard layout.
- [x] Show backend and Binance connection status.
- [x] Show active symbols and controls for adding/removing them.
- [x] Display the latest price and timestamp for each symbol.
- [ ] Display recent one-minute OHLC candles.
- [ ] Add candlestick or line charts.
- [ ] Connect to the backend WebSocket for live candle updates. Live tick
      streaming is connected; candle streaming will be added in Phase 4.
- [x] Automatically reconnect the frontend WebSocket after disconnection.
- [ ] Display current strategy signals and indicator values.
- [ ] Display Variant A and Variant B positions and P&L separately.
- [ ] Display the trade log and execution status.
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

### Phase 11: Testing

- [ ] Unit-test tick parsing and UTC timestamp normalization.
- [ ] Unit-test OHLC calculations and exact minute boundaries.
- [ ] Test late ticks, missing minutes, duplicate ticks, and symbol separation.
- [ ] Unit-test SMA/EMA calculations and crossover signals.
- [ ] Unit-test SL, TP, position P&L, and duplicate-signal prevention.
- [ ] Mock Binance WebSocket and REST responses for repeatable tests.
- [ ] Test disconnect and reconnection behavior.
- [ ] Test API success, validation, and failure responses.
- [ ] Test frontend API and WebSocket error handling.
- [ ] Run a controlled end-to-end test against Binance Testnet.

### Phase 12: Documentation and delivery

- [ ] Document prerequisites and exact setup commands.
- [ ] Document how to create Binance Testnet credentials.
- [ ] Document configuration fields without exposing credentials.
- [ ] Document the strategy, SL/TP calculations, and assumptions.
- [ ] Document how to start the backend and frontend.
- [ ] Provide example API requests and WebSocket messages.
- [ ] Add screenshots or a short demo recording if required.
- [ ] Document known limitations and future improvements.
- [ ] Verify that no production Binance endpoint or real credential is used.

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
