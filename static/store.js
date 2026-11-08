/* Sandbox storefront: PayPal JS SDK buttons -> server-side Orders API create/capture. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const EMOJI = { "KC-VASE-TALL": "🏺", "KC-BOWL-NEST": "🥣", "KC-PLATTER-L": "🍽️", "KC-CANDLE-2": "🕯️", "KC-MUG-SET4": "☕", "KC-PLANTER": "🪴", "KC-TEA-PAIR": "🍵", "KC-PITCHER": "🫗" };

  function toast(msg, kind = "info") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 6000);
  }

  async function post(path, body) {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || data.detail || "Request failed");
    return data;
  }

  function loadSdk(clientId) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&currency=USD&intent=capture&components=buttons`;
      s.onload = resolve;
      s.onerror = () => reject(new Error("Could not load the PayPal JS SDK"));
      document.head.appendChild(s);
    });
  }

  function showReceipt(r) {
    const o = r.order;
    $("#receipt").hidden = false;
    $("#receipt").innerHTML = `
      <strong>Payment captured.</strong> Order <code>${esc(o.order_id)}</code> · PayPal capture <code>${esc(r.capture_id)}</code> · ${esc(o.total)} ${esc(o.currency)}<br>
      Shipped to ${esc(o.shipping_address.line1)}, ${esc(o.shipping_address.city)} with USPS <code>${esc(o.fulfillment.tracking_number)}</code> (tracking pushed to PayPal: ${esc(r.tracking)}).
      <ol>
        <li>Open <a href="https://www.sandbox.paypal.com/myaccount/summary" target="_blank" rel="noopener">sandbox.paypal.com</a> and log in as the <em>same sandbox buyer</em>.</li>
        <li>Find this payment under Activity, choose <em>Report a problem</em>, and open a dispute (e.g. "I didn't receive it").</li>
        <li>Back on the <a href="/">dashboard</a>, the webhook (or ↻ Refresh) brings the dispute in, and the agent investigates it.</li>
      </ol>`;
    $("#receipt").scrollIntoView({ behavior: "smooth" });
  }

  async function boot() {
    const res = await fetch("/api/store/products");
    const data = await res.json();
    $("#products").innerHTML = data.products.map((p) => `
      <article class="product">
        <div class="swatch">${EMOJI[p.sku] || "🏺"}</div>
        <h3>${esc(p.title)}</h3>
        <p>${esc(p.description)}</p>
        <div class="price">$${esc(p.price)}</div>
        <div id="pp-${esc(p.sku)}"></div>
      </article>`).join("");

    if (data.mode !== "sandbox" || !data.client_id) {
      $("#notice").hidden = false;
      $("#notice").innerHTML = "The storefront is running in <strong>demo mode</strong>: add <code>PAYPAL_CLIENT_ID</code> and <code>PAYPAL_CLIENT_SECRET</code> (sandbox) to <code>.env</code> to enable real PayPal sandbox checkout. The dashboard's ⚡ Simulate dispute button works without credentials.";
      return;
    }
    try { await loadSdk(data.client_id); } catch (e) { toast(e.message, "error"); return; }

    for (const p of data.products) {
      window.paypal.Buttons({
        style: { layout: "horizontal", height: 38, tagline: false, label: "buynow" },
        createOrder: async () => (await post("/api/store/orders", { sku: p.sku })).id,
        onApprove: async (approval) => {
          try { showReceipt(await post(`/api/store/orders/${approval.orderID}/capture`)); toast("Payment captured in the PayPal sandbox"); }
          catch (e) { toast(e.message, "error"); }
        },
        onError: (err) => toast(`PayPal error: ${err?.message || err}`, "error"),
      }).render(`#pp-${p.sku}`);
    }
  }
  boot();
})();
