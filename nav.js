/* nav.js - shared sticky navbar + side drawer for every WiFi Sense page.
   <script src="/nav.js" defer></script> is enough: it injects nav.css, a skip link, the top bar
   (brand, page links, snooze bell, theme), and a side drawer (all pages + "On this page" sections,
   taken from elements with data-section="Label"). Theme cycles light -> dark -> high contrast. */
(function () {
  "use strict";
  const ICON = {
    home: '<path d="M3 11 12 3.5 21 11M5 9.5V20h14V9.5"/><path d="M10 20v-5h4v5"/>',
    devices: '<path d="M3 12V4.5A1.5 1.5 0 0 1 4.5 3H12l9 9-9 9z"/><circle cx="8" cy="8" r="1.4"/>',
    history: '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6L3.5 8.5"/><path d="M3.5 3.5v5h5M12 7.5V12l3 2"/>',
    health: '<path d="M3 12h4l2-4 4 8 2-4h6"/>',
    plan: '<rect x="3" y="3" width="18" height="18" rx="1.5"/><path d="M3 12h7v9M10 3v5M14 12h7M14 12v4"/>',
    rules: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.5 6H14a4 4 0 0 1 4 4v5.5"/><path d="m15.5 13 2.5 2.5 2.5-2.5"/>',
    tuning: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    kiosk: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    burger: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20.5a2 2 0 0 0 4 0"/>',
    bellOff: '<path d="M6 16V11a6 6 0 0 1 9.5-4.9M18 11v5l1.5 2H8M10 20.5a2 2 0 0 0 4 0M3 3l18 18"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    contrast: '<circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor"/>',
    dot: '<circle cx="12" cy="12" r="3"/>',
  };
  const svg = (k, extra = "") => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${ICON[k]}</svg>`;
  const PAGES = [
    ["/", "Dashboard", "home"], ["/devices", "Devices", "devices"], ["/history", "History", "history"],
    ["/health", "Wi-Fi health", "health"], ["/floorplan", "Floor plan", "plan"], ["/rules", "Rules", "rules"],
    ["/tuning", "Tuning lab", "tuning"], ["/kiosk", "Wall display", "kiosk"],
  ];
  const here = location.pathname.replace(/\.html$/, "").replace(/\/index$/, "/") || "/";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- styles (last in <head> so the contrast theme wins) ----------
  const css = document.createElement("link");
  css.rel = "stylesheet"; css.href = "/nav.css";
  document.head.appendChild(css);

  // ---------- theme: light -> dark -> contrast ----------
  const THEMES = ["light", "dark", "contrast"];
  const osDark = () => matchMedia("(prefers-color-scheme: dark)").matches;
  let theme = null;
  try { theme = localStorage.getItem("wifisense-theme"); } catch {}
  if (!THEMES.includes(theme)) theme = osDark() ? "dark" : "light";
  document.documentElement.dataset.theme = theme;

  function build() {
    const main = document.querySelector("main, .wrap");
    if (main && !main.id) main.id = "main";
    if (main) main.setAttribute("tabindex", "-1");

    const links = PAGES.map(([href, label, ic]) =>
      `<a href="${href}"${href === here ? ' aria-current="page"' : ""}>${svg(ic)}<span>${label}</span></a>`).join("");
    const bar = document.createElement("div");
    bar.innerHTML = `
      <a class="ws-skip" href="#main">Skip to content</a>
      <nav class="wsnav" aria-label="Main">
        <button class="ws-burger" id="ws-burger" aria-label="Open navigation" aria-expanded="false" aria-controls="ws-drawer">${svg("burger")}</button>
        <a class="ws-brand" href="/"><img src="/brand/logo_mark.png" alt="">WiFi Sense</a>
        <div class="ws-links">${links}</div>
        <div class="ws-right">
          <div style="position:relative">
            <button class="ws-ic" id="ws-bell" aria-haspopup="true" aria-expanded="false" aria-label="Alerts and snooze">${svg("bell")}<span class="ws-lbl" id="ws-bell-lbl">Alerts</span></button>
            <div class="ws-menu" id="ws-menu" role="menu"></div>
          </div>
          <button class="ws-ic" id="ws-theme" aria-label="Change theme"></button>
        </div>
      </nav>
      <div class="ws-shade" id="ws-shade"></div>
      <aside class="ws-drawer" id="ws-drawer" aria-label="Navigation" aria-hidden="true">
        <div class="ws-dh"><img src="/brand/logo_mark.png" alt=""><div><b>WiFi Sense</b><small>Smarter connectivity</small></div></div>
        <h4>Pages</h4>${links}
        <div class="ws-sec" id="ws-sec"></div>
      </aside>`;
    document.body.prepend(...bar.childNodes);

    // ----- theme button -----
    const tb = document.getElementById("ws-theme");
    const paintTheme = () => {
      const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
      tb.innerHTML = svg(theme === "dark" ? "moon" : theme === "contrast" ? "contrast" : "sun") + `<span class="ws-lbl">${theme[0].toUpperCase() + theme.slice(1)}</span>`;
      tb.title = `Theme: ${theme} (click for ${next})`;
    };
    tb.addEventListener("click", () => {
      theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
      document.documentElement.dataset.theme = theme;
      try { localStorage.setItem("wifisense-theme", theme); } catch {}
      paintTheme();
      window.dispatchEvent(new CustomEvent("ws-theme", { detail: theme }));
    });
    paintTheme();

    // ----- drawer -----
    const drawer = document.getElementById("ws-drawer"), shade = document.getElementById("ws-shade"),
          burger = document.getElementById("ws-burger");
    let spy = null;
    const openDrawer = open => {
      drawer.classList.toggle("open", open); shade.classList.toggle("open", open);
      drawer.setAttribute("aria-hidden", String(!open)); burger.setAttribute("aria-expanded", String(open));
      if (open) {
        const secs = [...document.querySelectorAll("[data-section]")].filter(el => el.offsetParent !== null);
        secs.forEach((el, i) => { if (!el.id) el.id = "sec-" + i; });
        document.getElementById("ws-sec").innerHTML = secs.length
          ? `<h4>On this page</h4>` + secs.map(el => `<a href="#${el.id}" data-sec="${el.id}">${svg("dot")}${esc(el.dataset.section)}</a>`).join("") : "";
        if (spy) spy.disconnect();
        spy = new IntersectionObserver(entries => entries.forEach(e => {
          const a = drawer.querySelector(`[data-sec="${e.target.id}"]`);
          if (a) a.classList.toggle("active", e.isIntersecting);
        }), { rootMargin: "-70px 0px -40% 0px" });
        secs.forEach(el => spy.observe(el));
        (drawer.querySelector("a[aria-current]") || drawer.querySelector("a")).focus();
      } else {
        if (spy) spy.disconnect();
        burger.focus();
      }
    };
    burger.addEventListener("click", () => openDrawer(!drawer.classList.contains("open")));
    shade.addEventListener("click", () => openDrawer(false));
    drawer.addEventListener("click", ev => { if (ev.target.closest("a")) openDrawer(false); });

    // ----- snooze bell -----
    const bell = document.getElementById("ws-bell"), menu = document.getElementById("ws-menu");
    const GROUP = { all: "All alerts", motion: "Movement", presence: "Presence", devices: "Devices & Wi-Fi" };
    let snooze = {};
    const until = t => { const d = new Date(t * 1000); return d.toDateString() === new Date().toDateString()
      ? d.toTimeString().slice(0, 5) : d.toLocaleDateString(undefined, { weekday: "short" }) + " " + d.toTimeString().slice(0, 5); };
    const active = () => Object.entries(snooze).filter(([, t]) => t * 1000 > Date.now());
    function paintBell() {
      const a = active();
      bell.classList.toggle("on", a.length > 0);
      bell.innerHTML = svg(a.length ? "bellOff" : "bell") + `<span class="ws-lbl">${a.length ? "Snoozed" : "Alerts"}</span>`;
      menu.innerHTML = `<div class="ws-state">${a.length ? a.map(([g, t]) => `${GROUP[g] || g} snoozed until <b>${until(t)}</b>`).join("<br>")
          : "Alerts are on. Security alerts always come through."}</div>
        <h4>Snooze all alerts</h4>
        <button role="menuitem" data-g="all" data-m="15">15 minutes</button><button role="menuitem" data-g="all" data-m="60">1 hour</button>
        <button role="menuitem" data-g="all" data-m="480">8 hours</button><button role="menuitem" data-g="all" data-m="tomorrow">Until tomorrow 07:00</button>
        <h4>Snooze only…</h4>
        <button role="menuitem" data-g="motion" data-m="60">Movement for 1 hour</button>
        <button role="menuitem" data-g="presence" data-m="60">Presence for 1 hour</button>
        <button role="menuitem" data-g="devices" data-m="60">Devices &amp; Wi-Fi for 1 hour</button>
        ${a.length ? `<h4>&nbsp;</h4><button role="menuitem" data-g="resume" data-m="0"><b>Resume all alerts</b></button>` : ""}`;
    }
    async function loadSnooze() {
      try { snooze = (await (await fetch("/api/snooze", { cache: "no-store" })).json()).snooze || {}; } catch {}
      paintBell();
    }
    const openMenu = open => { menu.classList.toggle("open", open); bell.setAttribute("aria-expanded", String(open));
      if (open) { const f = menu.querySelector("button"); if (f) f.focus(); } };
    bell.addEventListener("click", ev => { ev.stopPropagation(); openMenu(!menu.classList.contains("open")); });
    menu.addEventListener("click", async ev => {
      const b = ev.target.closest("button[data-g]"); if (!b) return;
      let minutes = b.dataset.m;
      if (minutes === "tomorrow") { const t = new Date(); t.setDate(t.getDate() + 1); t.setHours(7, 0, 0, 0); minutes = Math.round((t - Date.now()) / 60000); }
      try {
        const r = await fetch("/api/snooze", { method: "POST", headers: { "Content-Type": "application/json" },
                                                body: JSON.stringify({ group: b.dataset.g, minutes: +minutes }) });
        snooze = (await r.json()).snooze || {};
      } catch {}
      paintBell(); openMenu(false); bell.focus();
    });
    document.addEventListener("click", ev => { if (!ev.target.closest("#ws-menu")) openMenu(false); });
    loadSnooze(); setInterval(loadSnooze, 60000);

    // ----- keyboard -----
    document.addEventListener("keydown", ev => {
      if (ev.key === "Escape") { if (menu.classList.contains("open")) { openMenu(false); bell.focus(); } else if (drawer.classList.contains("open")) openDrawer(false); }
    });
    window.WSNav = { openDrawer, reloadSnooze: loadSnooze };
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build); else build();
})();
