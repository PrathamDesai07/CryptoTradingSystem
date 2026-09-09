const apiUrl = (path) => new URL(`../api/${path}`, window.location.href);
const socketUrl = () => {
  const url = new URL("../ws/ticks", window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return url;
};

const state = {
  ticks: new Map(),
  symbols: new Set(),
  cards: new Map(),
  pendingTicks: new Map(),
  frameScheduled: false,
  reconnectAttempt: 0,
  pollSeconds: 5,
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
};

async function request(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed: ${response.status}`);
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
    ? Number(tick.price).toLocaleString(undefined, { maximumFractionDigits: 8 })
    : "Waiting...";
  card.querySelector("small").textContent = tick
    ? new Date(tick.timestamp).toLocaleString()
    : "No update received";
}

function renderTickSnapshot() {
  elements.symbolCount.textContent = state.symbols.size;
  for (const symbol of state.symbols) updateTickCard(symbol);
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
}

function connectSocket() {
  const socket = new WebSocket(socketUrl());
  socket.addEventListener("open", () => {
    state.reconnectAttempt = 0;
    setConnection(true);
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type !== "ticks") return;
    for (const tick of message.data) queueTick(tick);
    const latest = message.data.at(-1);
    if (latest) elements.lastUpdate.textContent = new Date(latest.timestamp).toLocaleTimeString();
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
  if (!button) return;
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

async function initialize() {
  try {
    await Promise.all([loadPublicConfig(), loadDashboard()]);
    connectSocket();
    window.setInterval(() => loadDashboard().catch(() => setConnection(false)), state.pollSeconds * 1000);
  } catch (error) {
    elements.message.textContent = error.message;
    setConnection(false);
  }
}

initialize();
