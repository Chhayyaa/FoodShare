(() => {
  const cfg = window.APP_CONFIG || { mode: "local", apiUrl: "/api" };
  const LOCAL = cfg.mode === "local";
  const FOOD_TYPES = { cooked: "Cooked meals", raw: "Raw ingredients", bakery: "Bakery", packaged: "Packaged food", fruits_veg: "Fruits & vegetables", beverages: "Beverages", other: "Other" };
  const MAX_IMAGE = 5 * 1024 * 1024;

  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = (iso) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const opts = (map, sel) => Object.entries(map).map(([k, v]) => `<option value="${k}" ${k === sel ? "selected" : ""}>${esc(v)}</option>`).join("");
  const left = (h) => (h < 1 ? `${Math.max(1, Math.round(h * 60))} min left` : `${Math.floor(h)}h ${Math.round((h % 1) * 60)}m left`);
  const localInput = (d) => { const p = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; };

  const state = { session: load(), me: null, isAdmin: false, refresh: null, timer: null, authView: "login", pending: null, pos: null };

  function load() { try { return JSON.parse(localStorage.getItem("fw_session")); } catch { return null; } }
  function save(s) { state.session = s; if (s) localStorage.setItem("fw_session", JSON.stringify(s)); else localStorage.removeItem("fw_session"); }

  function toast(msg, error) {
    const t = $("#toast");
    t.textContent = msg; t.className = "show" + (error ? " error" : "");
    clearTimeout(toast.t); toast.t = setTimeout(() => (t.className = ""), 4000);
  }

  // ------------------------------------------------------------ auth
  async function cognito(target, payload) {
    const res = await fetch(`https://cognito-idp.${cfg.region}.amazonaws.com/`, {
      method: "POST",
      headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": "AWSCognitoIdentityProviderService." + target },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const e = new Error(data.message || "Authentication error"); e.code = (data.__type || "").split("#").pop(); throw e; }
    return data;
  }
  const jwt = (t) => JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));

  async function cloudLogin(email, password) {
    const r = await cognito("InitiateAuth", { AuthFlow: "USER_PASSWORD_AUTH", ClientId: cfg.clientId, AuthParameters: { USERNAME: email, PASSWORD: password } });
    const a = r.AuthenticationResult;
    if (!a) throw new Error("Unsupported sign-in challenge: " + r.ChallengeName);
    save({ email, idToken: a.IdToken, refreshToken: a.RefreshToken, exp: jwt(a.IdToken).exp });
  }

  async function getToken() {
    const s = state.session;
    if (!s) throw new Error("Not signed in");
    if (LOCAL) return `dev:${s.email}:${s.role}`;
    if (s.exp - 60 < Date.now() / 1000) {
      const r = await cognito("InitiateAuth", { AuthFlow: "REFRESH_TOKEN_AUTH", ClientId: cfg.clientId, AuthParameters: { REFRESH_TOKEN: s.refreshToken } });
      save({ ...s, idToken: r.AuthenticationResult.IdToken, exp: jwt(r.AuthenticationResult.IdToken).exp });
    }
    return state.session.idToken;
  }

  async function api(path, opts = {}) {
    const token = await getToken();
    const res = await fetch(cfg.apiUrl + path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) { logout(); throw new Error("Session expired, please sign in again"); }
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  function logout() { save(null); state.me = null; state.isAdmin = false; stopPolling(); renderAuth(); }

  // --------------------------------------------------------- layout
  function shell(inner) {
    stopPolling();
    const s = state.session;
    const role = state.isAdmin ? "admin" : state.me?.role || "";
    $("#app").innerHTML = `
      <header class="top">
        <div class="brand">🌱 FoodShare <small>Cloud-Based Food Waste Management</small></div>
        ${s ? `<div class="who">${esc(s.email)} <span class="chip">${esc(role)}</span> <button class="ghost" data-act="logout">Sign out</button></div>` : ""}
      </header>
      <main>${inner}</main>
      <footer>“Good Food Should Be Eaten, Not Wasted”</footer>`;
  }

  function startPolling(fn) {
    state.refresh = fn;
    state.timer = setInterval(() => { if (!document.hidden) fn().catch(() => {}); }, 15000);
  }
  function stopPolling() { clearInterval(state.timer); state.timer = null; state.refresh = null; }

  // ------------------------------------------------------------ auth UI
  function renderAuth() {
    if (LOCAL) {
      shell(`<section class="panel auth"><h2>Sign in (local demo)</h2>
        <p class="muted">No password in local mode. Try <b>canteen@demo.test</b> (provider), <b>ngo@demo.test</b> (receiver) or any email as admin.</p>
        <form data-form="local-login" class="grid">
          <label class="wide">Email<input name="email" type="email" required value="ngo@demo.test"></label>
          <label class="wide">I am a<select name="role"><option value="provider">Food provider</option><option value="receiver" selected>NGO / volunteer (receiver)</option><option value="admin">Admin</option></select></label>
          <button class="primary wide">Continue</button></form></section>`);
      return;
    }
    const v = state.authView;
    const tabs = `<div class="tabs"><button data-act="auth-view" data-v="login">Sign in</button><button data-act="auth-view" data-v="register">Register</button></div>`;
    let body;
    if (v === "register") {
      body = `<form data-form="register" class="grid">
        <label class="wide">Name / organisation<input name="name" required maxlength="100"></label>
        <label class="wide">Email<input name="email" type="email" required></label>
        <label class="wide">Password (8+ chars, upper, lower, number)<input name="password" type="password" minlength="8" required></label>
        <label class="wide">I am a<select name="role"><option value="provider">Food provider (canteen / restaurant / hostel / event)</option><option value="receiver">NGO / volunteer (food receiver)</option></select></label>
        <button class="primary wide">Create account</button></form>`;
    } else if (v === "confirm") {
      body = `<p class="muted">We emailed a verification code to ${esc(state.pending)}.</p>
        <form data-form="confirm" class="grid"><label class="wide">Code<input name="code" required inputmode="numeric"></label><button class="primary wide">Verify</button></form>`;
    } else {
      body = `<form data-form="login" class="grid">
        <label class="wide">Email<input name="email" type="email" required></label>
        <label class="wide">Password<input name="password" type="password" required></label>
        <button class="primary wide">Sign in</button></form>`;
    }
    shell(`<section class="panel auth"><h2>Welcome to FoodShare</h2>${v === "confirm" ? "" : tabs}${body}</section>`);
  }

  function renderProfile() {
    const roleName = state.session.role || "";
    shell(`<section class="panel auth"><h2>Complete your profile</h2>
      <form data-form="profile" class="grid">
        <label class="wide">Name / organisation<input name="name" required maxlength="100"></label>
        <label class="wide">Phone<input name="phone" maxlength="30"></label>
        <label class="wide">Address<input name="address" maxlength="300"></label>
        <label>Latitude<input name="latitude" type="number" step="any" required></label>
        <label>Longitude<input name="longitude" type="number" step="any" required></label>
        <button type="button" class="wide" data-act="geo">📍 Use my current location</button>
        <button class="primary wide">Save profile${roleName ? " as " + esc(roleName) : ""}</button>
      </form></section>`);
  }

  // --------------------------------------------------------- provider
  async function renderProvider() {
    const me = state.me;
    shell(`
      <section class="panel"><h2>Share surplus food</h2>
        <form data-form="listing" class="grid">
          <label>Food item<input name="title" required maxlength="120" placeholder="e.g. Veg biryani"></label>
          <label>Type<select name="food_type">${opts(FOOD_TYPES)}</select></label>
          <label>Quantity (servings)<input name="quantity" type="number" min="1" max="100000" required></label>
          <label>Best before / expiry<input name="expiry" type="datetime-local" required value="${localInput(new Date(Date.now() + 3 * 3600e3))}" min="${localInput(new Date(Date.now() + 10 * 60e3))}"></label>
          <label class="wide">Pickup address<input name="address" maxlength="300" value="${esc(me.address)}"></label>
          <label>Latitude<input name="latitude" type="number" step="any" required value="${me.latitude}"></label>
          <label>Longitude<input name="longitude" type="number" step="any" required value="${me.longitude}"></label>
          <button type="button" data-act="geo">📍 Use my current location</button>
          <label class="wide">Details (optional)<textarea name="description" maxlength="500"></textarea></label>
          <label class="wide">Photo (optional, max 5 MB)<input name="image" type="file" accept="image/jpeg,image/png,image/webp"></label>
          <button class="primary wide">Publish listing</button>
        </form></section>
      <section class="panel"><h2>My listings</h2><div id="list"></div></section>`);
    const refresh = async () => {
      const { listings } = await api("/listings/mine");
      $("#list").innerHTML = listings.length ? `<div class="cards">${listings.map(providerCard).join("")}</div>` : `<div class="empty">No listings yet. Share your first surplus food above.</div>`;
    };
    await refresh().catch((e) => toast(e.message, true));
    startPolling(refresh);
  }

  const cardImg = (l) => (l.image_url ? `<img src="${esc(l.image_url)}" alt="${esc(l.title)}" loading="lazy">` : `<div class="noimg">🍲</div>`);

  function providerCard(l) {
    let extra = "";
    if (l.request_id && l.request_status !== "cancelled") {
      extra = `<div class="note"><b>${esc(l.receiver_name)}</b> ${l.request_status === "completed" ? "collected this" : "is collecting this"}${l.receiver_phone ? " · " + esc(l.receiver_phone) : ""}<br>Pickup code: <b>${esc(l.request_id.slice(0, 6).toUpperCase())}</b> · <span class="chip ${l.request_status}">${l.request_status}</span></div>
        <div class="actions">${l.request_status === "confirmed" || l.request_status === "collected" ? `<button class="primary" data-act="complete" data-rid="${l.request_id}">Confirm handover completed</button>` : ""}${l.request_status === "confirmed" ? `<button class="danger" data-act="cancel" data-rid="${l.request_id}">Cancel pickup</button>` : ""}</div>`;
    }
    return `<article class="card">${cardImg(l)}<div class="card-body">
      <div class="row"><h3>${esc(l.title)}</h3><span class="chip ${l.status}">${l.status}</span></div>
      <p class="muted">${esc(FOOD_TYPES[l.food_type] || l.food_type)} · ${l.quantity} servings</p>
      <p class="muted">Expires ${fmtTime(l.expiry_time)}</p>${extra}</div></article>`;
  }

  // --------------------------------------------------------- receiver
  async function renderReceiver() {
    const me = state.me;
    state.pos = state.pos || { lat: me.latitude, lng: me.longitude };
    shell(`
      <section class="panel"><h2>Food available nearby</h2>
        <form data-form="filter" class="grid">
          <label>Search radius (km)<input name="radius" type="number" min="1" max="100" value="10"></label>
          <button type="button" data-act="geo-browse">📍 Use my current location</button>
          <button class="primary">Refresh</button>
        </form><br><div id="nearby"></div></section>
      <section class="panel"><h2>My requests</h2><div id="requests"></div></section>`);
    const refresh = async () => {
      const radius = $('[name="radius"]')?.value || 10;
      const [near, mine] = await Promise.all([
        api(`/listings?lat=${state.pos.lat}&lng=${state.pos.lng}&radius_km=${encodeURIComponent(radius)}`),
        api("/requests/mine"),
      ]);
      $("#nearby").innerHTML = near.count ? `<div class="cards">${near.listings.map(nearbyCard).join("")}</div>` : `<div class="empty">No food available nearby right now. We'll alert you when something is shared.</div>`;
      $("#requests").innerHTML = mine.requests.length ? `<div class="cards">${mine.requests.map(requestCard).join("")}</div>` : `<div class="empty">You haven't requested any food yet.</div>`;
    };
    state.refresh = refresh;
    await refresh().catch((e) => toast(e.message, true));
    startPolling(refresh);
  }

  function nearbyCard(l) {
    return `<article class="card">${cardImg(l)}<div class="card-body">
      <div class="row"><h3>${esc(l.title)}</h3><span class="chip">${l.distance_km} km</span></div>
      <p class="muted">${esc(FOOD_TYPES[l.food_type] || l.food_type)} · ${l.quantity} servings · by ${esc(l.provider_name)}</p>
      <p class="muted">${esc(l.address || "")}</p>
      ${l.description ? `<p class="muted">${esc(l.description)}</p>` : ""}
      <p class="muted"><b>${left(l.hours_left)}</b> (until ${fmtTime(l.expiry_time)})</p>
      <div class="actions"><button class="primary" data-act="request" data-id="${l.id}">Request / claim</button></div></div></article>`;
  }

  function requestCard(r) {
    const active = r.status === "confirmed";
    return `<article class="card">${cardImg(r)}<div class="card-body">
      <div class="row"><h3>${esc(r.title)}</h3><span class="chip ${r.status}">${r.status}</span></div>
      <p class="muted">${r.quantity} servings · ${esc(r.provider_name)}${r.provider_phone ? " · " + esc(r.provider_phone) : ""}</p>
      ${r.status !== "cancelled" ? `<div class="note">${esc(r.pickup_note)}<br>Pickup code: <b>${esc(r.pickup_code)}</b></div>` : ""}
      <div class="actions">
        ${active ? `<button class="primary" data-act="collect" data-rid="${r.id}">Mark as collected</button><button class="danger" data-act="cancel" data-rid="${r.id}">Cancel</button>` : ""}
        ${r.status === "collected" ? `<button class="primary" data-act="complete" data-rid="${r.id}">Confirm completed</button>` : ""}
      </div></div></article>`;
  }

  // ------------------------------------------------------------- admin
  async function renderAdmin() {
    shell(`
      <section class="panel"><h2>Impact overview</h2><div id="stats"></div></section>
      <section class="panel"><h2>Servings rescued (last 14 days)</h2><div id="chart"></div></section>
      <section class="panel"><div class="row"><h2>All food listings</h2><button data-act="csv">Export CSV</button></div><div id="alllist" class="scroll"></div></section>
      <section class="panel"><h2>Users</h2><div id="users" class="scroll"></div></section>`);
    const refresh = async () => {
      const [s, l, u] = await Promise.all([api("/admin/stats"), api("/admin/listings"), api("/admin/users")]);
      state.adminListings = l.listings;
      const rate = s.rescue_rate == null ? "–" : Math.round(s.rescue_rate * 100) + "%";
      const st = (v, label) => `<div class="stat"><b>${v}</b><span>${label}</span></div>`;
      $("#stats").innerHTML = `<div class="stats">${st(s.rescued_servings, "Servings rescued")}${st(s.wasted_servings, "Servings expired unclaimed")}${st(rate, "Rescue rate")}${st(s.listings.total, "Total listings")}${st(s.listings.available, "Available now")}${st(s.listings.booked, "Awaiting pickup")}${st(s.users.providers, "Providers")}${st(s.users.receivers, "NGOs / volunteers")}</div>`;
      const max = Math.max(1, ...s.daily.map((d) => Number(d.servings)));
      $("#chart").innerHTML = s.daily.length ? `<div class="chartwrap"><div class="bars">${s.daily.map((d) => `<div class="bar" style="height:${(Number(d.servings) / max) * 100}%" title="${esc(d.day)}: ${d.servings} servings"><span>${esc(d.day.slice(5))}</span></div>`).join("")}</div></div>` : `<div class="empty">No completed pickups yet.</div>`;
      $("#alllist").innerHTML = `<table><tr><th>Food</th><th>Provider</th><th>Servings</th><th>Expires</th><th>Status</th></tr>${l.listings.map((x) => `<tr><td>${esc(x.title)}</td><td>${esc(x.provider_name)}</td><td>${x.quantity}</td><td>${fmtTime(x.expiry_time)}</td><td><span class="chip ${x.status}">${x.status}</span></td></tr>`).join("")}</table>`;
      $("#users").innerHTML = `<table><tr><th>Name</th><th>Email</th><th>Role</th><th>Joined</th></tr>${u.users.map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.email)}</td><td>${esc(x.role)}</td><td>${fmtTime(x.created_at)}</td></tr>`).join("")}</table>`;
    };
    await refresh().catch((e) => toast(e.message, true));
    startPolling(refresh);
  }

  function exportCsv() {
    const cell = (v) => { let s = String(v ?? ""); if (/^[=+\-@]/.test(s)) s = "'" + s; return `"${s.replace(/"/g, '""')}"`; };
    const rows = [["Food", "Type", "Servings", "Provider", "Expires", "Status"], ...(state.adminListings || []).map((l) => [l.title, l.food_type, l.quantity, l.provider_name, l.expiry_time, l.status])];
    const blob = new Blob([rows.map((r) => r.map(cell).join(",")).join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "food-listings.csv"; a.click();
    URL.revokeObjectURL(a.href);
  }

  // ----------------------------------------------------------- routing
  async function start() {
    if (!state.session) return renderAuth();
    try {
      const me = await api("/me");
      state.isAdmin = me.is_admin; state.me = me.profile;
      if (!LOCAL) state.session.role = me.role;
      if (state.isAdmin) return renderAdmin();
      if (!me.profile) return renderProfile();
      return me.profile.role === "provider" ? renderProvider() : renderReceiver();
    } catch (e) {
      if (state.session) { toast(e.message, true); renderAuth(); }
    }
  }

  function getPosition() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) return reject(new Error("Geolocation is not supported by this browser"));
      navigator.geolocation.getCurrentPosition((p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude }), () => reject(new Error("Could not get your location - enter it manually")), { timeout: 10000 });
    });
  }

  async function uploadImage(file) {
    if (file.size > MAX_IMAGE) throw new Error("Image must be 5 MB or smaller");
    const p = await api("/uploads/presign", { method: "POST", body: { content_type: file.type } });
    const res = await fetch(p.upload_url, { method: "PUT", headers: p.headers, body: file });
    if (!res.ok) throw new Error("Image upload failed");
    return p.key;
  }

  // ------------------------------------------------------------ events
  const forms = {
    "local-login": async (f) => { save({ email: f.email.value.trim().toLowerCase(), role: f.role.value }); await start(); },
    login: async (f) => {
      try { await cloudLogin(f.email.value.trim().toLowerCase(), f.password.value); await start(); }
      catch (e) { if (e.code === "UserNotConfirmedException") { state.pending = f.email.value.trim().toLowerCase(); state.authView = "confirm"; renderAuth(); } else throw e; }
    },
    register: async (f) => {
      const email = f.email.value.trim().toLowerCase();
      await cognito("SignUp", { ClientId: cfg.clientId, Username: email, Password: f.password.value, UserAttributes: [{ Name: "email", Value: email }, { Name: "name", Value: f.name.value.trim() }, { Name: "custom:role", Value: f.role.value }] });
      state.pending = email; state.authView = "confirm"; renderAuth();
    },
    confirm: async (f) => { await cognito("ConfirmSignUp", { ClientId: cfg.clientId, Username: state.pending, ConfirmationCode: f.code.value.trim() }); state.authView = "login"; renderAuth(); toast("Email verified - please sign in"); },
    profile: async (f) => {
      await api("/me", { method: "POST", body: { name: f.name.value, phone: f.phone.value, address: f.address.value, latitude: f.latitude.value, longitude: f.longitude.value } });
      await start();
    },
    listing: async (f) => {
      const body = { title: f.title.value, food_type: f.food_type.value, quantity: f.quantity.value, expiry_time: new Date(f.expiry.value).toISOString(), address: f.address.value, latitude: f.latitude.value, longitude: f.longitude.value, description: f.description.value };
      if (f.image.files[0]) body.image_key = await uploadImage(f.image.files[0]);
      const r = await api("/listings", { method: "POST", body });
      toast(`Listed! ${r.notified} nearby NGO/volunteer(s) alerted.`);
      f.reset(); await state.refresh();
    },
    filter: async () => { await state.refresh(); },
  };

  const actions = {
    logout,
    "auth-view": (el) => { state.authView = el.dataset.v; renderAuth(); },
    geo: async (el) => {
      const p = await getPosition(); const f = el.closest("form");
      f.latitude.value = p.lat.toFixed(6); f.longitude.value = p.lng.toFixed(6);
    },
    "geo-browse": async () => { state.pos = await getPosition(); await state.refresh(); },
    request: async (el) => { const r = await api(`/listings/${el.dataset.id}/request`, { method: "POST" }); toast(`Confirmed! Pickup code ${r.request.pickup_code}`); await state.refresh(); },
    collect: async (el) => { await api(`/requests/${el.dataset.rid}/collect`, { method: "POST" }); toast("Marked as collected"); await state.refresh(); },
    complete: async (el) => { await api(`/requests/${el.dataset.rid}/complete`, { method: "POST" }); toast("Handover completed - thank you!"); await state.refresh(); },
    cancel: async (el) => { if (!confirm("Cancel this pickup?")) return; await api(`/requests/${el.dataset.rid}/cancel`, { method: "POST" }); toast("Pickup cancelled"); await state.refresh(); },
    csv: exportCsv,
  };

  document.addEventListener("submit", async (e) => {
    const form = e.target.closest("[data-form]");
    if (!form) return;
    e.preventDefault();
    const btn = form.querySelector("button:not([type=button])");
    if (btn) btn.disabled = true;
    try { await forms[form.dataset.form](form); } catch (err) { toast(err.message, true); }
    if (btn) btn.disabled = false;
  });

  document.addEventListener("click", async (e) => {
    const el = e.target.closest("[data-act]");
    if (!el || !actions[el.dataset.act]) return;
    el.disabled = true;
    try { await actions[el.dataset.act](el); } catch (err) { toast(err.message, true); }
    el.disabled = false;
  });

  start();
})();
