const apiUrl = (path) => new URL(`../api/${path}`, window.location.href);
const socketUrl = () => {
  const url = new URL("../ws/ticks", window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return url;
};

const TOKEN_KEY = "cts_token";
const getAuthToken = () => window.localStorage.getItem(TOKEN_KEY) || "";
const storeAuthToken = (token) => {
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
};

const state = {
  ticks: new Map(),
  symbols: new Set(),
  cards: new Map(),
  pendingTicks: new Map(),
  frameScheduled: false,
  reconnectAttempt: 0,
  pollSeconds: 5,
  candles: new Map(),
  socket: null,
  selectedSymbol: null,
  chart: null,
  candleSeries: null,
  volumeSeries: null,
  smaSeries: null,
  emaSeries: null,
  indicatorsEnabled: false,
  fastSmaPeriod: null,
  slowEmaPeriod: null,
  chartIntervals: ["1m"],
  selectedInterval: "1m",
  selectedIntervalSeconds: 60,
  lastChartBar: null,
  lastChartVolume: null,
  orderBookRenderIntervalMilliseconds: 500,
  pendingOrderBook: null,
  orderBookRenderTimer: null,
  lastOrderBookRenderAt: 0,
  orderExecutionEnabled: false,
  orderSide: "BUY",
  orderType: "LIMIT",
  credentialsConfigured: false,
  orderPnl: new Map(),
  pnlBasis: null,
  recentOrders: [],
  strategyOrderQuantity: null,
};
const elements = {
  title: document.querySelector("#app-title"),
  marketDataLabel: document.querySelector("#market-data-label"),
  badge: document.querySelector("#connection-badge"),
  stream: document.querySelector("#stream-state"),
  symbolCount: document.querySelector("#symbol-count"),
  lastUpdate: document.querySelector("#last-update"),
  orderState: document.querySelector("#order-state"),
  tickGrid: document.querySelector("#tick-grid"),
  featureGrid: document.querySelector("#feature-grid"),
  form: document.querySelector("#symbol-form"),
  input: document.querySelector("#symbol-input"),
  message: document.querySelector("#message"),
  detail: document.querySelector("#market-detail"),
  detailSymbol: document.querySelector("#detail-symbol"),
  detailPrice: document.querySelector("#detail-price"),
  detailChange: document.querySelector("#detail-change"),
  closeDetail: document.querySelector("#close-detail"),
  candleOpen: document.querySelector("#candle-open"),
  candleHigh: document.querySelector("#candle-high"),
  candleLow: document.querySelector("#candle-low"),
  candleClose: document.querySelector("#candle-close"),
  bookTime: document.querySelector("#book-time"),
  bidRows: document.querySelector("#bid-rows"),
  askRows: document.querySelector("#ask-rows"),
  chartContainer: document.querySelector("#price-chart"),
  bookMidPrice: document.querySelector("#book-mid-price"),
  marketTabs: document.querySelector("#market-tabs"),
  indicatorToggle: document.querySelector("#indicator-toggle"),
  indicatorLegend: document.querySelector("#indicator-legend"),
  smaPeriod: document.querySelector("#sma-period"),
  emaPeriod: document.querySelector("#ema-period"),
  smaValue: document.querySelector("#sma-value"),
  emaValue: document.querySelector("#ema-value"),
  latestSignal: document.querySelector("#latest-signal"),
  timeframeOptions: document.querySelector("#timeframe-options"),
  executionWarning: document.querySelector("#execution-warning"),
  buySide: document.querySelector("#buy-side"),
  sellSide: document.querySelector("#sell-side"),
  orderForm: document.querySelector("#order-form"),
  orderPrice: document.querySelector("#order-price"),
  orderQuantity: document.querySelector("#order-quantity"),
  orderQuoteQuantity: document.querySelector("#order-quote-quantity"),
  orderStopPrice: document.querySelector("#order-stop-price"),
  orderTif: document.querySelector("#order-tif"),
  conditionalType: document.querySelector("#conditional-type"),
  conditionalTypeField: document.querySelector("#conditional-type-field"),
  priceField: document.querySelector("#price-field"),
  stopPriceField: document.querySelector("#stop-price-field"),
  quoteSizeField: document.querySelector("#quote-size-field"),
  tifField: document.querySelector("#tif-field"),
  submitOrder: document.querySelector("#submit-order"),
  orderMessage: document.querySelector("#order-message"),
  positionList: document.querySelector("#position-list"),
  openOrderList: document.querySelector("#open-order-list"),
  recentOrderList: document.querySelector("#recent-order-list"),
  refreshOrders: document.querySelector("#refresh-orders"),
  credentialForm: document.querySelector("#credential-form"),
  runtimeApiKey: document.querySelector("#runtime-api-key"),
  runtimeApiSecret: document.querySelector("#runtime-api-secret"),
  cancelCredentials: document.querySelector("#cancel-credentials"),
  credentialMessage: document.querySelector("#credential-message"),
  realizedPnl: document.querySelector("#realized-pnl"),
  unrealizedPnl: document.querySelector("#unrealized-pnl"),
  totalPnl: document.querySelector("#total-pnl"),
  orderTicket: document.querySelector("#order-ticket"),
  closeOrderTicket: document.querySelector("#close-order-ticket"),
  showOrderTicket: document.querySelector("#show-order-ticket"),
  strategyOrderQuantity: document.querySelector("#strategy-order-quantity"),
  strategyOrderAsset: document.querySelector("#strategy-order-asset"),
  strategyQuantityStatus: document.querySelector("#strategy-quantity-status"),
  authScreen: document.querySelector("#auth-screen"),
  authTabLogin: document.querySelector("#auth-tab-login"),
  authTabSignup: document.querySelector("#auth-tab-signup"),
  authMessage: document.querySelector("#auth-message"),
  loginForm: document.querySelector("#login-form"),
  loginUsername: document.querySelector("#login-username"),
  loginPassword: document.querySelector("#login-password"),
  signupForm: document.querySelector("#signup-form"),
  signupUsername: document.querySelector("#signup-username"),
  signupPassword: document.querySelector("#signup-password"),
  signupApiKey: document.querySelector("#signup-api-key"),
  signupApiSecret: document.querySelector("#signup-api-secret"),
  userArea: document.querySelector("#user-area"),
  authUser: document.querySelector("#auth-user"),
  logoutButton: document.querySelector("#logout-button"),
  changeCredentialsButton: document.querySelector("#change-credentials-button"),
  credentialsSettings: document.querySelector("#credentials-settings"),
  closeCredentialsSettings: document.querySelector("#close-credentials-settings"),
  credentialsSettingsForm: document.querySelector("#credentials-settings-form"),
  credentialsPassword: document.querySelector("#credentials-password"),
  credentialsApiKey: document.querySelector("#credentials-api-key"),
  credentialsApiSecret: document.querySelector("#credentials-api-secret"),
  credentialsSettingsMessage: document.querySelector("#credentials-settings-message"),
  retryStoredCredentials: document.querySelector("#retry-stored-credentials"),
  accountBalanceStrip: document.querySelector("#account-balance-strip"),
  accountFreeBalance: document.querySelector("#account-free-balance"),
  accountInvestedBalance: document.querySelector("#account-invested-balance"),
  accountUtilization: document.querySelector("#account-utilization"),
};

async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 15000);
  const token = getAuthToken();
  let response;
  try {
    response = await fetch(apiUrl(path), {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.headers || {}),
      },
    });
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The backend request timed out. Check order status before retrying.");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
  const body = await response.json().catch(() => ({}));
  const detail = typeof body.detail === "object" ? body.detail?.message : body.detail;
  const authErrorCode = typeof body.detail === "object" ? body.detail?.code : null;
  const isApplicationAuthFailure = response.status === 401 && (
    authErrorCode === "AUTH_REQUIRED"
    || authErrorCode === "SESSION_EXPIRED"
    // Backward compatibility while a browser is connected to an older backend.
    || body.detail === "authentication required"
    || body.detail === "session is invalid or expired"
  );
  if (isApplicationAuthFailure && token) {
    storeAuthToken("");
    showAuthGate("login", "Your session expired. Sign in again.");
  }
  if (!response.ok) throw new Error(detail || `Request failed: ${response.status}`);
  return body;
}

function ensureTickCard(symbol) {
  let card = state.cards.get(symbol);
  if (card) return card;
  card = document.createElement("article");
  card.className = "tick-card";
  card.innerHTML = `
    <div><strong class="symbol"></strong><button class="remove" data-symbol="${symbol}" aria-label="Remove ${symbol}">Remove</button></div>
    <p class="price">Waiting...</p>
    <small>No update received</small>`;
  card.querySelector(".symbol").textContent = symbol;
  elements.tickGrid.append(card);
  state.cards.set(symbol, card);
  return card;
}

function updateTickCard(symbol) {
  const tick = state.ticks.get(symbol);
  const card = ensureTickCard(symbol);
  card.querySelector(".price").textContent = tick
    ? formatPrice(tick.price)
    : "Waiting...";
  card.querySelector("small").textContent = tick
    ? new Date(tick.timestamp).toLocaleString()
    : "No update received";
}

function renderTickSnapshot() {
  elements.symbolCount.textContent = state.symbols.size;
  for (const symbol of state.symbols) updateTickCard(symbol);
  if (state.selectedSymbol) renderMarketTabs();
}

function queueTick(tick) {
  state.pendingTicks.set(tick.symbol, tick);
  if (state.frameScheduled) return;
  state.frameScheduled = true;
  window.requestAnimationFrame(() => {
    for (const [symbol, latestTick] of state.pendingTicks) {
      state.ticks.set(symbol, latestTick);
      state.symbols.add(symbol);
      updateTickCard(symbol);
      if (symbol === state.selectedSymbol) {
        renderMarketHeader();
        updateLivePnl(Number(latestTick.price));
      }
    }
    state.pendingTicks.clear();
    state.frameScheduled = false;
    elements.symbolCount.textContent = state.symbols.size;
  });
}

function renderFeatures(features) {
  elements.featureGrid.replaceChildren();
  for (const [name, status] of Object.entries(features)) {
    const item = document.createElement("div");
    item.className = `feature ${status}`;
    item.innerHTML = `<span>${name.replaceAll("_", " ")}</span><strong>${status}</strong>`;
    elements.featureGrid.append(item);
  }
}

function setConnection(connected) {
  elements.badge.textContent = connected ? "Live" : "Reconnecting";
  elements.badge.className = `badge ${connected ? "online" : "offline"}`;
}

async function loadDashboard() {
  const data = await request("dashboard");
  state.symbols = new Set(data.symbols);
  state.ticks = new Map(Object.entries(data.ticks));
  state.candles = new Map(Object.entries(data.candles));
  elements.stream.textContent = data.health.binance_connected ? "Connected" : "Disconnected";
  elements.lastUpdate.textContent = data.health.last_message_at
    ? new Date(data.health.last_message_at).toLocaleTimeString()
    : "--";
  renderTickSnapshot();
  renderFeatures(data.features);
}

async function loadPublicConfig() {
  const config = await request("config");
  elements.title.textContent = config.app_title;
  elements.marketDataLabel.textContent = config.market_data_label;
  elements.orderState.textContent = config.order_execution_enabled ? "Enabled" : "Disabled";
  state.pollSeconds = config.tick_poll_seconds;
  state.orderBookRenderIntervalMilliseconds = config.order_book_render_interval_milliseconds;
  state.fastSmaPeriod = config.fast_sma_period;
  state.slowEmaPeriod = config.slow_ema_period;
  state.strategyOrderQuantity = Number(config.strategy_order_quantity);
  elements.strategyOrderQuantity.value = String(config.strategy_order_quantity);
  state.chartIntervals = Array.isArray(config.chart_intervals) && config.chart_intervals.length
    ? config.chart_intervals
    : ["1m"];
  if (!state.chartIntervals.includes(state.selectedInterval)) {
    state.selectedInterval = state.chartIntervals[0];
    state.selectedIntervalSeconds = INTERVAL_SECONDS[state.selectedInterval] || 60;
  }
  elements.smaPeriod.textContent = state.fastSmaPeriod;
  elements.emaPeriod.textContent = state.slowEmaPeriod;
  state.orderExecutionEnabled = Boolean(config.order_execution_enabled);
  elements.executionWarning.textContent = state.orderExecutionEnabled
    ? "Demo execution enabled"
    : "Demo execution disabled";
  elements.executionWarning.classList.toggle("enabled", state.orderExecutionEnabled);
  elements.submitOrder.disabled = false;
  renderTimeframeOptions();
}

async function saveStrategyOrderQuantity() {
  const quantity = Number(elements.strategyOrderQuantity.value);
  if (!Number.isFinite(quantity) || quantity <= 0) {
    elements.strategyQuantityStatus.textContent = "Invalid";
    return;
  }
  elements.strategyQuantityStatus.textContent = "Saving…";
  try {
    const response = await request("strategy/order-quantity", {
      method: "PUT",
      body: JSON.stringify({ quantity: elements.strategyOrderQuantity.value }),
    });
    state.strategyOrderQuantity = Number(response.quantity);
    elements.strategyOrderQuantity.value = String(response.quantity);
    elements.strategyQuantityStatus.textContent = "Saved";
  } catch (error) {
    elements.strategyQuantityStatus.textContent = error.message;
  }
}

elements.strategyOrderQuantity.addEventListener("change", saveStrategyOrderQuantity);
elements.strategyOrderQuantity.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    elements.strategyOrderQuantity.blur();
  }
});

async function loadOrderSession() {
  const session = await request("order-session");
  state.credentialsConfigured = Boolean(
    session.runtime_session_active
    || (session.credentials_configured && session.execution_enabled),
  );
  state.orderExecutionEnabled = Boolean(session.execution_enabled);
  renderOrderSession();
}

async function loadAccountBalance() {
  const response = await request("account");
  const balances = response.account?.balances || [];
  const usdt = balances.find((item) => item.asset === "USDT") || { free: "0", locked: "0" };
  const free = Number(usdt.free || 0);
  let invested = Number(usdt.locked || 0);
  for (const balance of balances) {
    if (balance.asset === "USDT") continue;
    const quantity = Number(balance.free || 0) + Number(balance.locked || 0);
    const tick = state.ticks.get(`${balance.asset}USDT`);
    if (quantity > 0 && tick) invested += quantity * Number(tick.price || 0);
  }
  const total = free + invested;
  const utilization = total > 0 ? invested / total * 100 : 0;
  elements.accountFreeBalance.textContent = `${formatPrice(free)} USDT`;
  elements.accountInvestedBalance.textContent = `${formatPrice(invested)} USDT`;
  elements.accountUtilization.textContent = `${utilization.toFixed(2)}%`;
  elements.accountUtilization.classList.toggle("warning", utilization >= 80);
  elements.accountBalanceStrip.hidden = false;
}

function renderOrderSession() {
  elements.executionWarning.textContent = state.orderExecutionEnabled
    ? "Demo session connected"
    : state.credentialsConfigured
      ? "Credentials need verification"
      : "Credentials required";
  elements.executionWarning.classList.toggle("enabled", state.orderExecutionEnabled);
  elements.submitOrder.textContent = state.orderExecutionEnabled
    ? `Place demo ${state.orderSide.toLowerCase()} order`
    : "Connect to place order";
  elements.orderState.textContent = state.orderExecutionEnabled ? "Enabled" : "Disabled";
}

function connectSocket() {
  const socket = new WebSocket(socketUrl());
  state.socket = socket;
  socket.addEventListener("open", () => {
    state.reconnectAttempt = 0;
    setConnection(true);
    if (state.selectedSymbol) subscribeDepth(state.selectedSymbol);
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "ticks") {
      for (const tick of message.data) queueTick(tick);
      const latest = message.data.at(-1);
      if (latest) elements.lastUpdate.textContent = new Date(latest.timestamp).toLocaleTimeString();
    } else if (message.type === "candles") {
      for (const candle of message.data) {
        state.candles.set(candle.symbol, candle);
        if (candle.symbol === state.selectedSymbol) updateSelectedChart(candle);
      }
    } else if (message.type === "order_book" && message.data.symbol === state.selectedSymbol) {
      queueOrderBook(message.data);
    } else if (message.type === "indicators" && state.indicatorsEnabled) {
      for (const indicator of message.data) {
        if (indicator.symbol === state.selectedSymbol) updateIndicator(indicator);
      }
    } else if (message.type === "signals") {
      const signal = message.data.find((item) => item.symbol === state.selectedSymbol);
      if (signal) renderLatestSignal(signal);
    } else if (message.type === "error") {
      elements.message.textContent = message.message;
    }
  });
  socket.addEventListener("close", () => {
    setConnection(false);
    const delay = Math.min(30000, 1000 * (2 ** state.reconnectAttempt));
    state.reconnectAttempt += 1;
    window.setTimeout(connectSocket, delay);
  });
  socket.addEventListener("error", () => socket.close());
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await request("symbols", {
      method: "POST",
      body: JSON.stringify({ symbol: elements.input.value }),
    });
    state.symbols.add(result.symbol);
    elements.input.value = "";
    elements.message.textContent = result.added ? `${result.symbol} added` : `${result.symbol} is already active`;
    renderTickSnapshot();
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

elements.tickGrid.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-symbol]");
  if (!button) {
    const card = event.target.closest(".tick-card");
    const symbol = card?.querySelector("button[data-symbol]")?.dataset.symbol;
    if (symbol) selectSymbol(symbol);
    return;
  }
  try {
    await request(`symbols/${encodeURIComponent(button.dataset.symbol)}`, { method: "DELETE" });
    state.symbols.delete(button.dataset.symbol);
    state.ticks.delete(button.dataset.symbol);
    state.cards.get(button.dataset.symbol)?.remove();
    state.cards.delete(button.dataset.symbol);
    elements.message.textContent = `${button.dataset.symbol} removed`;
    elements.symbolCount.textContent = state.symbols.size;
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

function sendSocket(message) {
  if (state.socket?.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(message));
  }
}

function subscribeDepth(symbol) {
  sendSocket({ action: "subscribe_depth", symbol });
}

function unsubscribeDepth(symbol) {
  sendSocket({ action: "unsubscribe_depth", symbol });
}

async function selectSymbol(symbol) {
  if (state.selectedSymbol === symbol && !elements.detail.hidden) return;
  if (state.selectedSymbol && state.selectedSymbol !== symbol) {
    unsubscribeDepth(state.selectedSymbol);
  }
  state.selectedSymbol = symbol;
  document.body.classList.add("trading-mode");
  elements.detail.hidden = false;
  elements.detailSymbol.textContent = symbol;
  elements.detail.querySelector(".market-heading").insertBefore(
    elements.accountBalanceStrip,
    elements.detail.querySelector(".detail-actions"),
  );
  elements.strategyOrderAsset.textContent = symbol.endsWith("USDT")
    ? symbol.slice(0, -4)
    : "units";
  renderMarketHeader();
  renderMarketTabs();
  elements.bookTime.textContent = "Waiting for Binance...";
  elements.bidRows.replaceChildren();
  elements.askRows.replaceChildren();
  elements.bookMidPrice.textContent = "--";
  clearPendingOrderBook();
  renderCandle(state.candles.get(symbol));
  if (!initializeChart()) return;
  state.candleSeries.setData([]);
  state.volumeSeries.setData([]);
  state.smaSeries.setData([]);
  state.emaSeries.setData([]);
  subscribeDepth(symbol);
  try {
    await loadChartCandles(symbol);
    if (state.indicatorsEnabled) await loadIndicators(symbol);
    await refreshOrderManagement();
  } catch (error) {
    elements.message.textContent = error.message;
  }
  elements.detail.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderCandle(candle) {
  elements.candleOpen.textContent = candle ? formatPrice(candle.open) : "--";
  elements.candleHigh.textContent = candle ? formatPrice(candle.high) : "--";
  elements.candleLow.textContent = candle ? formatPrice(candle.low) : "--";
  elements.candleClose.textContent = candle ? formatPrice(candle.close) : "--";
  if (candle && state.candleSeries) state.candleSeries.update(toChartCandle(candle));
  if (candle) renderMarketHeader();
}

async function loadChartCandles(symbol) {
  const response = await request(
    `chart-candles/${encodeURIComponent(symbol)}?interval=${encodeURIComponent(state.selectedInterval)}`,
  );
  const chartCandles = response.candles;
  if (state.selectedSymbol !== symbol) return;
  const seriesData = chartCandles.map(toChartCandle);
  const volumeData = chartCandles.map(toVolumePoint);
  state.lastChartBar = seriesData.at(-1) || null;
  state.lastChartVolume = volumeData.at(-1) || null;
  state.candleSeries.setData(seriesData);
  state.volumeSeries.setData(volumeData);
  state.chart.timeScale().fitContent();
  const candle = chartCandles.at(-1);
  if (candle) renderCandleDetails(candle);
}

function updateSelectedChart(candle) {
  if (state.selectedInterval === "1m") {
    renderCandle(candle);
    state.lastChartBar = toChartCandle(candle);
    updateChartVolume(candle, state.lastChartBar.time);
    return;
  }
  const minute = toChartCandle(candle);
  const bucketTime = minute.time - (minute.time % state.selectedIntervalSeconds);
  let bar;
  if (state.lastChartBar?.time === bucketTime) {
    bar = {
      ...state.lastChartBar,
      high: Math.max(state.lastChartBar.high, minute.high),
      low: Math.min(state.lastChartBar.low, minute.low),
      close: minute.close,
    };
  } else {
    bar = { ...minute, time: bucketTime };
  }
  state.lastChartBar = bar;
  state.candleSeries.update(bar);
  updateChartVolume(candle, bucketTime);
  renderCandleDetails(bar);
}

function updateChartVolume(candle, time) {
  if (candle.volume === null || candle.volume === undefined) return;
  const incomingVolume = Number(candle.volume);
  const value = state.lastChartVolume?.time === time && state.selectedInterval !== "1m"
    ? state.lastChartVolume.value + incomingVolume
    : incomingVolume;
  const point = {
    time,
    value,
    color: Number(candle.close) >= Number(candle.open)
      ? "rgba(57, 217, 138, .42)"
      : "rgba(255, 77, 109, .42)",
  };
  state.lastChartVolume = point;
  state.volumeSeries.update(point);
}

function renderCandleDetails(candle) {
  elements.candleOpen.textContent = formatPrice(candle.open);
  elements.candleHigh.textContent = formatPrice(candle.high);
  elements.candleLow.textContent = formatPrice(candle.low);
  elements.candleClose.textContent = formatPrice(candle.close);
  renderMarketHeader();
}

const INTERVAL_SECONDS = Object.freeze({
  "1m": 60,
  "5m": 300,
  "15m": 900,
  "1h": 3600,
  "4h": 14400,
  "1d": 86400,
  "1w": 604800,
});

function intervalLabel(interval) {
  return interval.endsWith("h") || interval.endsWith("d") || interval.endsWith("w")
    ? interval.toUpperCase()
    : interval;
}

function renderTimeframeOptions() {
  const fragment = document.createDocumentFragment();
  for (const interval of state.chartIntervals) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = interval === state.selectedInterval
      ? "timeframe-button active"
      : "timeframe-button";
    button.dataset.interval = interval;
    button.textContent = intervalLabel(interval);
    button.setAttribute("aria-pressed", String(interval === state.selectedInterval));
    fragment.append(button);
  }
  elements.timeframeOptions.replaceChildren(fragment);
  syncIndicatorAvailability();
}

function syncIndicatorAvailability() {
  const available = state.selectedInterval === "1m";
  elements.indicatorToggle.querySelector(".indicator-icon").textContent = "∿";
  if (!available && state.indicatorsEnabled) {
    state.indicatorsEnabled = false;
    elements.indicatorLegend.hidden = true;
    state.smaSeries?.applyOptions({ visible: false });
    state.emaSeries?.applyOptions({ visible: false });
  }
  elements.indicatorToggle.disabled = !available;
  elements.indicatorToggle.classList.toggle("active", state.indicatorsEnabled);
  elements.indicatorToggle.setAttribute("aria-pressed", String(state.indicatorsEnabled));
  elements.indicatorToggle.title = available
    ? "Show or hide the one-minute SMA and EMA"
    : "SMA and EMA strategy indicators are available on the 1-minute chart";
  elements.indicatorToggle.lastChild.textContent = available
    ? ` SMA/EMA ${state.indicatorsEnabled ? "on" : "off"}`
    : " SMA/EMA · 1m";
}

elements.timeframeOptions.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-interval]");
  if (!button || button.dataset.interval === state.selectedInterval) return;
  state.selectedInterval = button.dataset.interval;
  state.selectedIntervalSeconds = INTERVAL_SECONDS[state.selectedInterval] || 60;
  state.lastChartBar = null;
  state.lastChartVolume = null;
  renderTimeframeOptions();
  if (!state.selectedSymbol || !initializeChart()) return;
  state.candleSeries.setData([]);
  state.volumeSeries.setData([]);
  state.smaSeries.setData([]);
  state.emaSeries.setData([]);
  try {
    await loadChartCandles(state.selectedSymbol);
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

function renderOrderBook(book) {
  renderLevels(elements.bidRows, book.bids, "bid");
  renderLevels(elements.askRows, [...book.asks].reverse(), "ask");
  if (book.bids.length && book.asks.length) {
    elements.bookMidPrice.textContent = formatPrice(
      (Number(book.bids[0].price) + Number(book.asks[0].price)) / 2,
    );
  }
  elements.bookTime.textContent = `Update ${book.last_update_id} · ${new Date(book.timestamp).toLocaleTimeString()}`;
}

function queueOrderBook(book) {
  // O(1): every incoming snapshot replaces the pending snapshot. Rendering is
  // trailing-edge throttled, so the next paint always uses the newest data.
  state.pendingOrderBook = book;
  if (state.orderBookRenderTimer !== null) return;

  const elapsed = performance.now() - state.lastOrderBookRenderAt;
  const delay = Math.max(0, state.orderBookRenderIntervalMilliseconds - elapsed);
  state.orderBookRenderTimer = window.setTimeout(() => {
    state.orderBookRenderTimer = null;
    const latestBook = state.pendingOrderBook;
    state.pendingOrderBook = null;
    if (!latestBook || latestBook.symbol !== state.selectedSymbol) return;
    renderOrderBook(latestBook);
    state.lastOrderBookRenderAt = performance.now();
  }, delay);
}

function clearPendingOrderBook() {
  if (state.orderBookRenderTimer !== null) {
    window.clearTimeout(state.orderBookRenderTimer);
  }
  state.orderBookRenderTimer = null;
  state.pendingOrderBook = null;
  state.lastOrderBookRenderAt = 0;
}

function renderLevels(container, levels, side) {
  const fragment = document.createDocumentFragment();
  for (const level of levels) {
    const row = document.createElement("tr");
    row.className = side;
    const price = document.createElement("td");
    const quantity = document.createElement("td");
    const actions = document.createElement("td");
    price.textContent = formatPrice(level.price);
    quantity.textContent = formatQuantity(level.quantity);
    actions.className = "depth-actions";
    actions.innerHTML = `<button class="depth-action buy" type="button" data-trade-side="BUY" data-price="${level.price}" title="Buy at this price">B</button><button class="depth-action sell" type="button" data-trade-side="SELL" data-price="${level.price}" title="Sell at this price">S</button>`;
    row.append(price, quantity, actions);
    fragment.append(row);
  }
  container.replaceChildren(fragment);
}

function setOrderSide(side) {
  state.orderSide = side;
  elements.buySide.classList.toggle("active", side === "BUY");
  elements.sellSide.classList.toggle("active", side === "SELL");
  elements.submitOrder.classList.toggle("buy", side === "BUY");
  elements.submitOrder.classList.toggle("sell", side === "SELL");
  renderOrderSession();
}

function setOrderType(type) {
  state.orderType = type;
  const conditional = type === "CONDITIONAL";
  document.querySelectorAll("[data-order-type]").forEach((button) => {
    const active = type === "CONDITIONAL"
      ? button.dataset.orderType === "STOP_LOSS_LIMIT"
      : button.dataset.orderType === type;
    button.classList.toggle("active", active);
  });
  const effectiveType = conditional ? elements.conditionalType.value : type;
  const market = effectiveType === "MARKET";
  const marketTrigger = ["STOP_LOSS", "TAKE_PROFIT"].includes(effectiveType);
  elements.conditionalTypeField.hidden = !conditional;
  elements.stopPriceField.hidden = !conditional;
  elements.priceField.hidden = market || marketTrigger;
  elements.tifField.hidden = market || marketTrigger || effectiveType === "LIMIT_MAKER";
  elements.quoteSizeField.hidden = !market || state.orderSide !== "BUY";
  elements.orderPrice.required = !elements.priceField.hidden;
}

function prefillDepthOrder(side, price) {
  openOrderTicket();
  setOrderSide(side);
  setOrderType("LIMIT");
  elements.orderPrice.value = price;
  elements.orderQuantity.focus();
  elements.orderMessage.textContent = `${side} limit prefilled from market depth.`;
}

function handleDepthAction(event) {
  const action = event.target.closest("button[data-trade-side]");
  if (action) prefillDepthOrder(action.dataset.tradeSide, action.dataset.price);
}

elements.bidRows.addEventListener("click", handleDepthAction);
elements.askRows.addEventListener("click", handleDepthAction);
elements.buySide.addEventListener("click", () => { setOrderSide("BUY"); setOrderType(state.orderType); });
elements.sellSide.addEventListener("click", () => { setOrderSide("SELL"); setOrderType(state.orderType); });
document.querySelectorAll("[data-order-type]").forEach((button) => button.addEventListener("click", () => {
  setOrderType(button.dataset.orderType === "STOP_LOSS_LIMIT" ? "CONDITIONAL" : button.dataset.orderType);
}));
elements.conditionalType.addEventListener("change", () => setOrderType("CONDITIONAL"));

elements.orderForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selectedSymbol) return;
  if (!state.orderExecutionEnabled) {
    elements.credentialForm.hidden = false;
    elements.credentialMessage.textContent = "Connect Binance Demo keys or retry the stored keys to enable order placement.";
    (state.credentialsConfigured ? elements.retryStoredCredentials : elements.runtimeApiKey).focus();
    return;
  }
  const type = state.orderType === "CONDITIONAL" ? elements.conditionalType.value : state.orderType;
  const usesQuote = type === "MARKET" && state.orderSide === "BUY" && elements.orderQuoteQuantity.value.trim();
  if (!usesQuote && !elements.orderQuantity.value.trim()) {
    elements.orderMessage.textContent = "Enter an order size.";
    elements.orderQuantity.focus();
    return;
  }
  if (!elements.priceField.hidden && !elements.orderPrice.value.trim()) {
    elements.orderMessage.textContent = "Enter a limit price.";
    elements.orderPrice.focus();
    return;
  }
  if (!elements.stopPriceField.hidden && !elements.orderStopPrice.value.trim()) {
    elements.orderMessage.textContent = "Enter a trigger price.";
    elements.orderStopPrice.focus();
    return;
  }
  const payload = { symbol: state.selectedSymbol, side: state.orderSide, type };
  if (!elements.priceField.hidden) payload.price = elements.orderPrice.value;
  if (!elements.stopPriceField.hidden) payload.stopPrice = elements.orderStopPrice.value;
  if (!elements.tifField.hidden) payload.timeInForce = elements.orderTif.value;
  if (usesQuote) {
    payload.quoteOrderQty = elements.orderQuoteQuantity.value;
  } else {
    payload.quantity = elements.orderQuantity.value;
  }
  elements.submitOrder.disabled = true;
  elements.orderMessage.textContent = "Submitting signed Demo Mode order...";
  try {
    const response = await request("orders", { method: "POST", body: JSON.stringify(payload) });
    elements.orderMessage.textContent = `Order ${response.order.orderId || response.order.clientOrderId || "accepted"}: ${response.order.status || "submitted"}`;
    subscribeDepth(state.selectedSymbol);
    await refreshOrderManagement();
    await loadAccountBalance().catch((error) => {
      elements.accountFreeBalance.textContent = "Unavailable";
      elements.accountInvestedBalance.textContent = "-- USDT";
      elements.accountUtilization.textContent = "--%";
      elements.accountBalanceStrip.hidden = false;
      elements.message.textContent = error.message;
    });
  } catch (error) {
    elements.orderMessage.textContent = error.message;
  } finally {
    elements.submitOrder.disabled = false;
  }
});

elements.credentialForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const apiKey = elements.runtimeApiKey.value;
  const apiSecret = elements.runtimeApiSecret.value;
  elements.credentialMessage.textContent = "Verifying with Binance Demo Mode...";
  const submit = elements.credentialForm.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    await request("order-session", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey, api_secret: apiSecret }),
    });
    elements.runtimeApiKey.value = "";
    elements.runtimeApiSecret.value = "";
    state.credentialsConfigured = true;
    state.orderExecutionEnabled = true;
    elements.credentialForm.hidden = true;
    elements.credentialMessage.textContent = "";
    renderOrderSession();
    elements.orderForm.requestSubmit();
  } catch (error) {
    elements.runtimeApiSecret.value = "";
    elements.credentialMessage.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

elements.cancelCredentials.addEventListener("click", () => {
  elements.runtimeApiKey.value = "";
  elements.runtimeApiSecret.value = "";
  elements.credentialForm.hidden = true;
  elements.credentialMessage.textContent = "";
});

async function refreshOrderManagement() {
  if (!state.selectedSymbol) return;
  const positions = await request(`positions?symbol=${encodeURIComponent(state.selectedSymbol)}`);
  renderPositions(positions.positions);
  const recent = await request(`orders?symbol=${encodeURIComponent(state.selectedSymbol)}&limit=20`);
  try {
    const pnl = await request(`pnl/${encodeURIComponent(state.selectedSymbol)}`);
    renderPnl(pnl);
  } catch (error) {
    state.orderPnl.clear();
    state.pnlBasis = null;
    elements.realizedPnl.textContent = "--";
    elements.unrealizedPnl.textContent = "--";
    elements.totalPnl.textContent = "--";
  }
  state.recentOrders = recent.orders;
  renderRecentOrders(state.recentOrders);
  try {
    const open = await request(`orders/open?symbol=${encodeURIComponent(state.selectedSymbol)}`);
    renderOpenOrders(open.orders);
  } catch (error) {
    elements.openOrderList.innerHTML = `<small>${escapeHtml(error.message)}</small>`;
  }
}

function renderPnl(pnl) {
  state.orderPnl = new Map(pnl.orders.map((item) => [String(item.order_id), item]));
  state.pnlBasis = {
    realized: Number(pnl.realized_pnl || 0),
    openQuantity: Number(pnl.open_quantity || 0),
    openCost: Number(pnl.open_cost || 0),
    orders: new Map(pnl.orders.map((item) => {
      const quantity = Number(item.remaining_quantity || 0);
      const unrealized = Number(item.unrealized_pnl || 0);
      return [String(item.order_id), {
        quantity,
        cost: Number(pnl.current_price) * quantity - unrealized,
        realized: item.realized_pnl === null ? null : Number(item.realized_pnl),
      }];
    })),
  };
  updateLivePnl(Number(pnl.current_price));
}

function updateLivePnl(currentPrice) {
  if (!state.pnlBasis || !Number.isFinite(currentPrice)) return;
  const unrealized = currentPrice * state.pnlBasis.openQuantity - state.pnlBasis.openCost;
  setPnlValue(elements.realizedPnl, state.pnlBasis.realized);
  setPnlValue(elements.unrealizedPnl, unrealized);
  setPnlValue(elements.totalPnl, state.pnlBasis.realized + unrealized);
  document.querySelectorAll("[data-order-pnl]").forEach((element) => {
    const basis = state.pnlBasis.orders.get(element.dataset.orderPnl);
    if (!basis) return;
    const value = basis.quantity > 0
      ? currentPrice * basis.quantity - basis.cost
      : basis.realized;
    if (value !== null) setPnlValue(element, value);
  });
}

function setPnlValue(element, value) {
  const number = Number(value || 0);
  element.textContent = `${number >= 0 ? "+" : ""}${formatPrice(number)} USDT`;
  element.className = number >= 0 ? "pnl-positive" : "pnl-negative";
}

function renderRecentOrders(orders) {
  if (!orders.length) {
    elements.recentOrderList.innerHTML = "<small>No orders submitted during this backend session</small>";
    return;
  }
  elements.recentOrderList.replaceChildren(...[...orders].reverse().map((order) => {
    const row = document.createElement("div");
    row.className = "management-row";
    const status = String(order.status || "SUBMITTED");
    const quantity = order.executedQty && Number(order.executedQty) > 0
      ? order.executedQty
      : order.origQty || order.quantity || order.quoteOrderQty || "--";
    const quote = Number(order.cummulativeQuoteQty || 0);
    const executed = Number(order.executedQty || 0);
    const average = quote > 0 && executed > 0 ? quote / executed : Number(order.price || 0);
    const identifier = order.orderId || order.clientOrderId || order.newClientOrderId || "--";
    const pnl = state.orderPnl.get(String(order.orderId));
    const pnlValue = pnl?.realized_pnl ?? pnl?.unrealized_pnl;
    const pnlText = pnlValue === null || pnlValue === undefined
      ? ""
      : ` · P&amp;L <span data-order-pnl="${escapeHtml(order.orderId)}" class="${Number(pnlValue) >= 0 ? "pnl-positive" : "pnl-negative"}">${Number(pnlValue) >= 0 ? "+" : ""}${formatPrice(pnlValue)}</span>`;
    const squareOff = status === "FILLED" && order.side === "BUY" && Number(pnl?.remaining_quantity || 0) > 0
      ? `<button type="button" data-close-quantity="${Number(pnl.remaining_quantity)}" data-order-id="${escapeHtml(order.orderId)}">Square off</button>`
      : "";
    row.innerHTML = `<div><strong>${escapeHtml(order.side)} ${escapeHtml(order.type)} · ${escapeHtml(quantity)}</strong><br><span>#${escapeHtml(identifier)}${average > 0 ? ` @ ${formatPrice(average)}` : ""}${pnlText}</span></div><div class="order-row-actions"><span class="order-status ${status.toLowerCase()}">${escapeHtml(status)}</span>${squareOff}</div>`;
    return row;
  }));
}

elements.recentOrderList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-close-quantity]");
  if (!button || !state.selectedSymbol) return;
  button.disabled = true;
  elements.orderMessage.textContent = "Submitting immediate square-off MARKET sell...";
  try {
    const orderId = button.dataset.orderId;
    const response = await request(`orders/${encodeURIComponent(state.selectedSymbol)}/${encodeURIComponent(orderId)}/square-off`, { method: "POST" });
    const settlement = response.settlement;
    elements.orderMessage.textContent = settlement
      ? `Square-off filled: sold ${formatQuantity(settlement.base_quantity_sold)} · ${formatPrice(settlement.quote_credited)} ${settlement.quote_asset} credited.`
      : `Square-off order ${response.order.orderId}: ${response.order.status}.`;
    await refreshOrderManagement();
    await loadAccountBalance();
  } catch (error) {
    elements.orderMessage.textContent = error.message;
    button.disabled = false;
  }
});

function openOrderTicket() {
  elements.orderTicket.hidden = false;
  elements.showOrderTicket.hidden = true;
}

function closeOrderTicket() {
  elements.orderTicket.hidden = true;
  elements.showOrderTicket.hidden = false;
  elements.credentialForm.hidden = true;
  elements.runtimeApiKey.value = "";
  elements.runtimeApiSecret.value = "";
}

elements.closeOrderTicket.addEventListener("click", closeOrderTicket);
elements.showOrderTicket.addEventListener("click", openOrderTicket);

function renderPositions(positions) {
  const open = positions.filter((position) => position.status === "OPEN");
  if (!open.length) {
    elements.positionList.innerHTML = "<small>No open strategy positions</small>";
    return;
  }
  elements.positionList.replaceChildren(...open.map((position) => {
    const row = document.createElement("div");
    row.className = "management-row";
    row.innerHTML = `<div><strong>Variant ${position.variant} · ${formatQuantity(position.quantity)}</strong><br><span>P&amp;L ${formatPrice(position.current_pnl)}</span></div><button type="button" data-square-off="${position.variant}">Square off</button>`;
    return row;
  }));
}

function renderOpenOrders(orders) {
  if (!orders.length) {
    elements.openOrderList.innerHTML = "<small>No open Binance orders</small>";
    return;
  }
  elements.openOrderList.replaceChildren(...orders.map((order) => {
    const row = document.createElement("div");
    row.className = "management-row";
    row.innerHTML = `<div><strong>${escapeHtml(order.side)} ${escapeHtml(order.type)}</strong><br><span>${escapeHtml(order.origQty)} @ ${escapeHtml(order.price)}</span></div><button type="button" data-cancel-order="${order.orderId}">Cancel</button>`;
    return row;
  }));
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

elements.positionList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-square-off]");
  if (!button || !state.selectedSymbol) return;
  try {
    await request(`positions/${encodeURIComponent(state.selectedSymbol)}/${button.dataset.squareOff}/square-off`, { method: "POST" });
    elements.orderMessage.textContent = `Variant ${button.dataset.squareOff} square-off submitted.`;
    await refreshOrderManagement();
  } catch (error) { elements.orderMessage.textContent = error.message; }
});

elements.openOrderList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-cancel-order]");
  if (!button || !state.selectedSymbol) return;
  try {
    await request("orders", { method: "DELETE", body: JSON.stringify({ symbol: state.selectedSymbol, order_id: Number(button.dataset.cancelOrder) }) });
    elements.orderMessage.textContent = `Order ${button.dataset.cancelOrder} canceled.`;
    await refreshOrderManagement();
  } catch (error) { elements.orderMessage.textContent = error.message; }
});

elements.refreshOrders.addEventListener("click", () => refreshOrderManagement().catch((error) => {
  elements.orderMessage.textContent = error.message;
}));

setOrderSide("BUY");
setOrderType("LIMIT");

document.addEventListener("keydown", (event) => {
  if (!state.selectedSymbol || elements.detail.hidden) return;
  const editing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (event.key === "Escape") {
    elements.orderForm.reset();
    setOrderType("LIMIT");
    elements.orderMessage.textContent = "Order ticket cleared.";
  } else if (event.ctrlKey && event.key === "Enter") {
    event.preventDefault();
    elements.orderForm.requestSubmit();
  } else if (!editing && event.key.toLowerCase() === "b") {
    openOrderTicket();
    setOrderSide("BUY");
  } else if (!editing && event.key.toLowerCase() === "s") {
    openOrderTicket();
    setOrderSide("SELL");
  } else if (!editing && event.key.toLowerCase() === "l") {
    openOrderTicket();
    setOrderType("LIMIT");
  } else if (!editing && event.key.toLowerCase() === "m") {
    openOrderTicket();
    setOrderType("MARKET");
  }
});

function formatNumber(value) {
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 8 });
}

function formatPrice(value) {
  const number = Number(value);
  const magnitude = Math.abs(number);
  const decimals = magnitude >= 1000 ? 2 : magnitude >= 1 ? 4 : 8;
  return number.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function formatQuantity(value) {
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 6 });
}

function renderMarketHeader() {
  if (!state.selectedSymbol) return;
  const tick = state.ticks.get(state.selectedSymbol);
  const candle = state.candles.get(state.selectedSymbol);
  elements.detailPrice.textContent = tick ? formatPrice(tick.price) : "--";
  if (!tick || !candle) {
    elements.detailChange.textContent = "Waiting for live price";
    elements.detailChange.className = "";
    return;
  }
  const open = Number(candle.open);
  const change = Number(tick.price) - open;
  const percent = open === 0 ? 0 : (change / open) * 100;
  elements.detailChange.textContent = `${change >= 0 ? "+" : ""}${formatPrice(change)}  (${percent >= 0 ? "+" : ""}${percent.toFixed(2)}%)`;
  elements.detailChange.className = change >= 0 ? "positive" : "negative";
}

function renderMarketTabs() {
  const fragment = document.createDocumentFragment();
  for (const symbol of state.symbols) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = symbol === state.selectedSymbol ? "market-tab active" : "market-tab";
    button.dataset.symbol = symbol;
    const tick = state.ticks.get(symbol);
    button.textContent = tick ? `${symbol}  ${formatPrice(tick.price)}` : symbol;
    fragment.append(button);
  }
  elements.marketTabs.replaceChildren(fragment);
}

elements.marketTabs.addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-symbol]");
  if (tab) selectSymbol(tab.dataset.symbol);
});

function toChartCandle(candle) {
  return {
    time: Math.floor(new Date(candle.interval_start).getTime() / 1000),
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
  };
}

function toVolumePoint(candle) {
  return {
    time: Math.floor(new Date(candle.interval_start).getTime() / 1000),
    value: Number(candle.volume || 0),
    color: Number(candle.close) >= Number(candle.open)
      ? "rgba(57, 217, 138, .42)"
      : "rgba(255, 77, 109, .42)",
  };
}

function initializeChart() {
  if (state.chart) return true;
  if (!window.LightweightCharts) {
    elements.message.textContent = "The chart library could not be loaded.";
    return false;
  }
  state.chart = LightweightCharts.createChart(elements.chartContainer, {
    autoSize: true,
    layout: {
      background: { type: "solid", color: "#091424" },
      textColor: "#91a3bd",
      attributionLogo: true,
    },
    grid: {
      vertLines: { color: "#17263b" },
      horzLines: { color: "#17263b" },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: {
      borderColor: "#24344d",
      scaleMargins: { top: 0.08, bottom: 0.27 },
    },
    timeScale: {
      borderColor: "#24344d",
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 4,
    },
  });
  state.candleSeries = state.chart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: "#39d98a",
    downColor: "#ff4d6d",
    borderVisible: false,
    wickUpColor: "#39d98a",
    wickDownColor: "#ff4d6d",
    priceLineVisible: true,
  });
  state.volumeSeries = state.chart.addSeries(LightweightCharts.HistogramSeries, {
    priceFormat: { type: "volume" },
    priceScaleId: "volume",
    lastValueVisible: false,
    priceLineVisible: false,
  });
  state.chart.priceScale("volume").applyOptions({
    scaleMargins: { top: 0.78, bottom: 0 },
    borderVisible: false,
  });
  state.smaSeries = state.chart.addSeries(LightweightCharts.LineSeries, {
    color: "#f1c40f",
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
    visible: state.indicatorsEnabled,
  });
  state.emaSeries = state.chart.addSeries(LightweightCharts.LineSeries, {
    color: "#d66efd",
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
    visible: state.indicatorsEnabled,
  });
  return true;
}

async function loadIndicators(symbol) {
  const response = await request(`indicators/${encodeURIComponent(symbol)}?limit=120`);
  if (state.selectedSymbol !== symbol || !state.indicatorsEnabled) return;
  const smaData = [];
  const emaData = [];
  for (const point of response.indicators) {
    const time = Math.floor(new Date(point.timestamp).getTime() / 1000);
    if (point.sma !== null) smaData.push({ time, value: Number(point.sma) });
    if (point.ema !== null) emaData.push({ time, value: Number(point.ema) });
  }
  state.smaSeries.setData(smaData);
  state.emaSeries.setData(emaData);
  const latest = response.indicators.at(-1);
  if (latest) renderIndicatorValues(latest);
  const latestSignals = Object.values(response.latest_signals);
  if (latestSignals.length) renderLatestSignal(latestSignals.at(-1));
}

function updateIndicator(indicator) {
  const time = Math.floor(new Date(indicator.timestamp).getTime() / 1000);
  if (indicator.sma !== null) state.smaSeries.update({ time, value: Number(indicator.sma) });
  if (indicator.ema !== null) state.emaSeries.update({ time, value: Number(indicator.ema) });
  renderIndicatorValues(indicator);
}

function renderIndicatorValues(indicator) {
  elements.smaValue.textContent = indicator.sma === null ? "--" : formatPrice(indicator.sma);
  elements.emaValue.textContent = indicator.ema === null ? "--" : formatPrice(indicator.ema);
}

function renderLatestSignal(signal) {
  elements.latestSignal.textContent = `${signal.action} · ${signal.variant} · ${formatPrice(signal.price)}`;
  elements.latestSignal.className = signal.action === "BUY" ? "buy" : "exit";
}

elements.indicatorToggle.addEventListener("click", async () => {
  if (state.selectedInterval !== "1m") return;
  state.indicatorsEnabled = !state.indicatorsEnabled;
  syncIndicatorAvailability();
  elements.indicatorLegend.hidden = !state.indicatorsEnabled;
  if (!initializeChart()) return;
  state.smaSeries.applyOptions({ visible: state.indicatorsEnabled });
  state.emaSeries.applyOptions({ visible: state.indicatorsEnabled });
  if (state.indicatorsEnabled && state.selectedSymbol) {
    await loadIndicators(state.selectedSymbol).catch((error) => {
      elements.message.textContent = error.message;
    });
  }
});

elements.closeDetail.addEventListener("click", () => {
  if (state.selectedSymbol) unsubscribeDepth(state.selectedSymbol);
  state.selectedSymbol = null;
  clearPendingOrderBook();
  elements.detail.hidden = true;
  document.body.classList.remove("trading-mode");
  document.querySelector(".header-actions").prepend(elements.accountBalanceStrip);
});

let dashboardStarted = false;

async function startDashboard() {
  if (dashboardStarted) return;
  dashboardStarted = true;
  try {
    await Promise.all([loadPublicConfig(), loadDashboard()]);
    await loadOrderSession();
    await loadAccountBalance().catch((error) => {
      elements.accountFreeBalance.textContent = "Unavailable";
      elements.accountInvestedBalance.textContent = "-- USDT";
      elements.accountUtilization.textContent = "--%";
      elements.accountBalanceStrip.hidden = false;
      elements.message.textContent = error.message;
    });
    connectSocket();
    window.setInterval(() => loadDashboard().catch(() => setConnection(false)), state.pollSeconds * 1000);
    window.setInterval(() => loadAccountBalance().catch(() => {}), 15000);
  } catch (error) {
    elements.message.textContent = error.message;
    setConnection(false);
  }
}

function setAuthTab(tab) {
  const login = tab === "login";
  elements.loginForm.hidden = !login;
  elements.signupForm.hidden = login;
  elements.authTabLogin.classList.toggle("active", login);
  elements.authTabSignup.classList.toggle("active", !login);
}

function showAuthGate(tab = "login", message = "") {
  storeAuthToken("");
  elements.userArea.hidden = true;
  elements.accountBalanceStrip.hidden = true;
  elements.authMessage.classList.remove("error");
  setAuthTab(tab);
  elements.authMessage.textContent = message;
  elements.authScreen.hidden = false;
}

async function enterDashboard(result) {
  const name = result?.user?.display_name || result?.user?.username || "";
  elements.authUser.textContent = name || "User";
  elements.authMessage.textContent = "";
  elements.authScreen.hidden = true;
  elements.userArea.hidden = false;
  await startDashboard();
}

async function submitAuth(route, payload, button) {
  button.disabled = true;
  elements.authMessage.classList.remove("error");
  elements.authMessage.textContent = "Please wait...";
  try {
    const result = await request(route, { method: "POST", body: JSON.stringify(payload) });
    storeAuthToken(result.token);
    await enterDashboard(result);
  } catch (error) {
    elements.authMessage.classList.add("error");
    elements.authMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

elements.authTabLogin.addEventListener("click", () => { elements.authMessage.textContent = ""; setAuthTab("login"); });
elements.authTabSignup.addEventListener("click", () => { elements.authMessage.textContent = ""; setAuthTab("signup"); });

elements.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitAuth("auth/login", {
    username: elements.loginUsername.value.trim(),
    password: elements.loginPassword.value,
  }, elements.loginForm.querySelector("button[type='submit']"));
});

elements.signupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitAuth("auth/signup", {
    username: elements.signupUsername.value.trim(),
    password: elements.signupPassword.value,
    api_key: elements.signupApiKey.value.trim(),
    api_secret: elements.signupApiSecret.value.trim(),
  }, elements.signupForm.querySelector("button[type='submit']"));
});

elements.logoutButton.addEventListener("click", async () => {
  try {
    await request("auth/logout", { method: "POST" });
  } catch (_) {
    // The session is already invalid; the reload below clears local state.
  }
  storeAuthToken("");
  window.location.reload();
});

function closeCredentialsSettings() {
  elements.credentialsSettings.hidden = true;
  elements.credentialsSettingsForm.reset();
  elements.credentialsSettingsMessage.textContent = "";
}

elements.changeCredentialsButton.addEventListener("click", () => {
  elements.credentialsSettings.hidden = false;
  elements.credentialsPassword.focus();
});
elements.closeCredentialsSettings.addEventListener("click", closeCredentialsSettings);
elements.credentialsSettings.addEventListener("click", (event) => {
  if (event.target === elements.credentialsSettings) closeCredentialsSettings();
});
elements.credentialsSettingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = elements.credentialsSettingsForm.querySelector("button[type='submit']");
  submit.disabled = true;
  elements.credentialsSettingsMessage.textContent = "Verifying replacement credentials...";
  try {
    await request("auth/credentials", {
      method: "PUT",
      body: JSON.stringify({
        password: elements.credentialsPassword.value,
        api_key: elements.credentialsApiKey.value.trim(),
        api_secret: elements.credentialsApiSecret.value.trim(),
      }),
    });
    state.credentialsConfigured = true;
    state.orderExecutionEnabled = true;
    renderOrderSession();
    await loadAccountBalance();
    elements.credentialsSettingsMessage.textContent = "Credentials updated successfully.";
    window.setTimeout(closeCredentialsSettings, 900);
  } catch (error) {
    elements.credentialsSettingsMessage.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

elements.retryStoredCredentials.addEventListener("click", async () => {
  elements.credentialMessage.textContent = "Verifying stored keys with Binance Demo Mode...";
  try {
    await request("auth/verify-binance", { method: "POST" });
    elements.credentialMessage.textContent = "";
    elements.credentialForm.hidden = true;
    state.credentialsConfigured = true;
    state.orderExecutionEnabled = true;
    renderOrderSession();
  } catch (error) {
    elements.credentialMessage.textContent = error.message;
  }
});

async function bootstrap() {
  if (!getAuthToken()) {
    showAuthGate("login");
    return;
  }
  try {
    const result = await request("auth/me");
    await enterDashboard(result);
  } catch (_) {
    showAuthGate("login", "Your session expired. Sign in again.");
  }
}

bootstrap();
