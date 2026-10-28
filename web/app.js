(() => {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("t") || "";
  if (params.has("t")) {
    const cleanUrl = window.location.pathname + window.location.hash;
    window.history.replaceState(null, "", cleanUrl);
  }

  const dot = document.getElementById("connection-dot");
  const connectionLabel = document.getElementById("connection-label");
  const health = document.getElementById("health");
  const events = document.getElementById("events");
  const requestStatus = document.getElementById("request-status");
  const task = document.getElementById("task");
  const askDialog = document.getElementById("ask-dialog");
  const askText = document.getElementById("ask-text");
  const askOptions = document.getElementById("ask-options");
  const answerText = document.getElementById("answer-text");
  let socket;
  let currentAsk = null;

  async function api(path, options = {}) {
    const headers = Object.assign({ "X-Token": token }, options.headers || {});
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, Object.assign({}, options, { headers }));
    const text = await response.text();
    let value;
    try { value = text ? JSON.parse(text) : {}; } catch (_error) { value = { detail: text }; }
    if (!response.ok) throw new Error(value.detail || `HTTP ${response.status}`);
    return value;
  }

  function appendEvent(event) {
    events.textContent += `${JSON.stringify(event)}\n`;
    if (events.textContent.length > 180000) {
      events.textContent = events.textContent.slice(-140000);
    }
    events.scrollTop = events.scrollHeight;
    if (event.type === "ask") showAsk(event);
  }

  function setConnected(connected) {
    dot.classList.toggle("online", connected);
    dot.classList.toggle("offline", !connected);
    dot.setAttribute("aria-label", connected ? "Connected" : "Disconnected");
    connectionLabel.textContent = connected ? "WebSocket connected" : "WebSocket disconnected";
  }

  async function loadHealth() {
    try {
      const value = await api("/api/health");
      health.textContent = JSON.stringify(value, null, 2);
    } catch (error) {
      health.textContent = `Health check failed: ${error.message}`;
    }
  }

  function connect() {
    if (!token) {
      setConnected(false);
      connectionLabel.textContent = "Missing session token; reopen from launcher";
      return;
    }
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${scheme}//${window.location.host}/ws?t=${encodeURIComponent(token)}`);
    socket.addEventListener("open", () => setConnected(true));
    socket.addEventListener("message", (message) => {
      try { appendEvent(JSON.parse(message.data)); }
      catch (_error) { appendEvent({ type: "client_error", text: "Invalid WebSocket event" }); }
    });
    socket.addEventListener("close", () => {
      setConnected(false);
      window.setTimeout(connect, 1200);
    });
    socket.addEventListener("error", () => setConnected(false));
  }

  function showAsk(event) {
    currentAsk = event;
    document.getElementById("ask-title").textContent = event.kind === "permission" ? "Permission request" : "Question";
    askText.textContent = event.text || "The agent needs a response.";
    askOptions.replaceChildren();
    answerText.value = "";
    const options = Array.isArray(event.options) ? event.options : [];
    options.forEach((option) => {
      const value = typeof option === "string" ? option : (option.value ?? option.label ?? JSON.stringify(option));
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = typeof option === "string" ? option : (option.label || String(value));
      button.addEventListener("click", () => { answerText.value = String(value); submitAnswer(); });
      askOptions.appendChild(button);
    });
    if (typeof askDialog.showModal === "function") askDialog.showModal();
    else askDialog.setAttribute("open", "open");
  }

  async function submitAnswer() {
    if (!currentAsk) return;
    const pending = currentAsk;
    const raw = answerText.value.trim();
    let answer = raw;
    if (pending.kind === "permission" && /^(yes|allow|approve)$/i.test(raw)) answer = true;
    if (pending.kind === "permission" && /^(no|deny|reject)$/i.test(raw)) answer = false;
    try {
      await api("/api/answer", { method: "POST", body: JSON.stringify({ id: pending.id, answer }) });
      currentAsk = null;
      askDialog.close();
    } catch (error) {
      requestStatus.textContent = `Answer failed: ${error.message}`;
    }
  }

  document.getElementById("run").addEventListener("click", async () => {
    requestStatus.textContent = "Starting…";
    try {
      const value = await api("/api/run", { method: "POST", body: JSON.stringify({ task: task.value }) });
      requestStatus.textContent = JSON.stringify(value);
    } catch (error) {
      requestStatus.textContent = `Run failed: ${error.message}`;
    }
  });

  document.getElementById("stop").addEventListener("click", async () => {
    try {
      requestStatus.textContent = JSON.stringify(await api("/api/stop", { method: "POST", body: "{}" }));
    } catch (error) {
      requestStatus.textContent = `Stop failed: ${error.message}`;
    }
  });

  document.getElementById("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitAnswer();
  });
  document.getElementById("answer-cancel").addEventListener("click", () => {
    answerText.value = "deny";
    submitAnswer();
  });

  loadHealth();
  window.setInterval(loadHealth, 5000);
  connect();
})();
