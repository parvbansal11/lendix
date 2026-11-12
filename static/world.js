(() => {
  const S = document.getElementById("stage");
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  const P = 3;                 // stage pixels per art pixel
  const W = 533, H = 300;      // art canvas

  const AGENTS = {
    "Front Desk":   {x:250,  y:470, role:"Greets every request and routes it", tools:["route","list_loans","get_loan","log_grievance"], hair:"#2b2118", skin:"#e0ac82", shirt:"#3B4A5E", pants:"#2a2f3a", glow:"#7cc8ff"},
    "Intake":       {x:470,  y:255, role:"Collects documents and consent", tools:["parse_upload","validate_application","request_document"], hair:"#5a3a22", skin:"#c68e62", shirt:"#2a5a96", pants:"#2b2f38", glow:"#7cc8ff"},
    "Risk":         {x:720,  y:255, role:"Runs fraud and identity checks", tools:["run_risk_checks"], hair:"#1c1c1c", skin:"#8d5a3b", shirt:"#c0602f", pants:"#3a3330", glow:"#ffb070"},
    "Credit":       {x:970,  y:255, role:"Works out what the borrower can afford", tools:["compute_affordability","bureau_lookup"], hair:"#7a5230", skin:"#f0c39b", shirt:"#2f7d78", pants:"#29333a", glow:"#7affd8"},
    "Underwriter":  {x:1050, y:475, role:"Decides, by the lender's written policy", tools:["apply_policy","draft_decision_note","escalate"], hair:"#dcd7ca", skin:"#c68e62", shirt:"#2a2d35", pants:"#1d2026", glow:"#ffd66b"},
    "Compliance":   {x:720,  y:690, role:"Checks every message and payout against RBI rules", tools:["check_message","check_document","check_disbursement","make_kfs"], hair:"#2b2118", skin:"#e0ac82", shirt:"#5c7a42", pants:"#2f3328", glow:"#b6ff8a"},
    "Disbursement": {x:1235, y:255, role:"Pays out to the borrower's own account", tools:["make_kfs","simulate_payout"], hair:"#a0522d", skin:"#f0c39b", shirt:"#34589a", pants:"#262b36", glow:"#7cc8ff"},
    "Collections":  {x:1235, y:690, role:"Schedules, receipts and fair-hours reminders", tools:["emi_schedule","record_payment","draft_reminder","request_restructure"], hair:"#1c1c1c", skin:"#a56b45", shirt:"#7a5482", pants:"#2c2630", glow:"#e2a2ff"},
    "Reporter":     {x:470,  y:690, role:"Turns results into files, charts and updates", tools:["make_document","make_chart","post_update","mention"], hair:"#3a2a1a", skin:"#d79f73", shirt:"#a8802f", pants:"#3a3226", glow:"#ffd66b"},
    "Risk head":    {x:1450, y:475, role:"A person. Sees only the exceptions", tools:["approve","decline","override"], hair:"#3a2418", skin:"#d9a27a", shirt:"#1f2a44", pants:"#1a1f2c", human:true},
  };
  const ROOMS = [
    [380,170,700,200,"Application floor","#56698a"], [1120,170,230,200,"Payouts","#58766a"],
    [150,395,200,180,"Front desk","#8a6a52"], [935,395,230,180,"Decision room","#6a5880"],
    [1360,395,180,180,"Risk team","#4f5866"], [380,600,450,175,"Records and checks","#62764f"],
    [1120,600,230,175,"Repayments","#7c5862"],
  ];
  const SHELF = {x:250, y:790};
  const POLICY = [["Auto-decline", "A hard fraud flag or FOIR above 50%."], ["Auto-approve", "Low risk up to ₹2 lakh within the FOIR cap."], ["Escalate", "Every other case goes to the risk head with its note."]];

  const el = (tag, cls, style) => { const e = document.createElement(tag); e.className = cls; Object.assign(e.style, style || {}); return e; };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function shade(hex, d) { const n = parseInt(hex.slice(1), 16); const c = v => Math.max(0, Math.min(255, Math.round(v + d))); return "#" + [c(n >> 16), c((n >> 8) & 255), c(n & 255)].map(v => v.toString(16).padStart(2, "0")).join(""); }
  let seed = 11; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;

  // ================= background =================
  const bg = el("canvas", "world"); bg.width = W; bg.height = H; S.append(bg);
  const g = bg.getContext("2d");
  const R = (x, y, w, h, c) => { g.fillStyle = c; g.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h)); };
  const A = a => a / P;

  function tree(x, y, s) {
    g.fillStyle = "rgba(0,0,0,.22)"; g.beginPath(); g.ellipse(x + 2, y + s + 2, s * .9, s * .35, 0, 0, 7); g.fill();
    R(x - 1, y + s * .4, 3, s * .8, "#5a3b22"); R(x, y + s * .4, 1, s * .8, "#6e4a2b");
    [["#24461f", 0, 0, 1], ["#2f5a27", -1, -1, .86], ["#3d7231", -2, -2, .66], ["#55923f", -3, -3, .38]].forEach(([c, dx, dy, k]) => { g.fillStyle = c; g.beginPath(); g.arc(x + dx, y + dy, s * k, 0, 7); g.fill(); });
  }
  function plant(x, y, kind) {
    R(x - 3, y - 1, 7, 6, "#a24d27"); R(x - 3, y - 1, 7, 1, "#c4663a"); R(x - 2, y + 5, 5, 1, "#7a3a1e");
    if (kind === 0) { [[-4, -4], [3, -5], [0, -8], [-2, -6], [2, -2]].forEach(([dx, dy], i) => { R(x + dx - 1, y + dy, 3, 4, i % 2 ? "#3e8f45" : "#5bb05e"); }); }
    else if (kind === 1) { g.fillStyle = "#2f7a3a"; g.beginPath(); g.arc(x + .5, y - 5, 5, 0, 7); g.fill(); g.fillStyle = "#4fa257"; g.beginPath(); g.arc(x - .5, y - 6, 3, 0, 7); g.fill(); R(x + 1, y - 9, 1, 1, "#e85a7a"); R(x - 3, y - 4, 1, 1, "#f2d15a"); }
    else { for (let i = 0; i < 5; i++) R(x - 4 + i * 2, y - 10 + (i % 2) * 2, 1, 10 - (i % 2) * 2, i % 2 ? "#3f8a3e" : "#5aa64f"); }
  }
  function drawWorld() {
    R(0, 0, W, H, "#4b7a37");
    for (let i = 0; i < 5200; i++) R(rnd() * W, rnd() * H, 1, 1, ["#436f31", "#558a40", "#4f8139", "#3f6a2e"][i % 4]);
    for (let i = 0; i < 180; i++) { const x = rnd() * W, y = rnd() * H; R(x, y, 1, 2, "#6aa04d"); R(x + 1, y + 1, 1, 1, "#6aa04d"); }
    for (let i = 0; i < 90; i++) { const x = rnd() * W, y = rnd() * H; R(x, y, 1, 1, ["#f2e47a", "#f6f3ea", "#ec8ea6", "#b9a6f2"][i % 4]); }
    for (let x = 0; x < 16; x += 4) for (let y = 147; y < 170; y += 4) { const c = rnd() < .5 ? "#bdb5a5" : "#aaa292"; R(x + (y % 8 ? 2 : 0), y, 3, 3, c); R(x + (y % 8 ? 2 : 0), y + 2, 3, 1, shade(c, -25)); }
    for (let x = 4; x < W; x += 15) { tree(x + rnd() * 5, 9 + rnd() * 5, 8 + rnd() * 2); tree(x + 7 + rnd() * 5, H - 9 - rnd() * 4, 8 + rnd() * 2); }
    R(8, 26, W - 16, 248, "#34363d");
    R(11, 38, W - 22, 233, "#a8744a");
    for (let y = 38; y < 271; y += 6) {
      const off = (y / 6) % 3 * 13;
      for (let x = 11 - off; x < W - 11; x += 40) {
        const sx = Math.max(11, x), w = Math.min(40, W - 11 - sx);
        const c = ["#a8744a", "#a06d44", "#ad7a4f", "#9c6a42"][Math.floor(rnd() * 4)];
        R(sx, y, w - 1, 5, c); R(sx, y + 5, w, 1, "#7d5233"); R(sx, y, 1, 5, "#8a5c3a");
        if (rnd() < .18) R(sx + 10 + rnd() * 20, y + 2, 2, 1, "#86593a");
      }
    }
    R(11, 26, W - 22, 12, "#6c707b"); R(11, 36, W - 22, 2, "#4b4e57"); R(11, 26, W - 22, 1, "#7e828d");
    for (let x = 30; x < W - 30; x += 52) {
      R(x - 1, 27, 26, 9, "#3c3f47"); R(x, 28, 24, 7, "#a6dcef"); R(x, 28, 24, 2, "#d7f1fa"); R(x + 11, 28, 2, 7, "#3c3f47");
      g.fillStyle = "rgba(255,236,190,.09)"; g.beginPath(); g.moveTo(x, 38); g.lineTo(x + 24, 38); g.lineTo(x + 40, 92); g.lineTo(x + 16, 92); g.closePath(); g.fill();
    }
    R(258, 28, 14, 7, "#e9e2cf"); R(259, 29, 12, 5, "#5d7fa6"); R(261, 31, 4, 3, "#e8c66a");
    g.fillStyle = "#e9e2cf"; g.beginPath(); g.arc(495, 31, 4, 0, 7); g.fill(); R(495, 28, 1, 3, "#222"); R(495, 31, 2, 1, "#222");
    R(8, 26, 3, 248, "#4b4e57"); R(W - 11, 26, 3, 248, "#4b4e57"); R(8, 271, W - 16, 3, "#4b4e57");
    R(8, 147, 3, 24, "#a8744a"); R(11, 147, 6, 24, "#8f6b4a");
    for (const [l, t, w, h, , c] of ROOMS) {
      const x = A(l), y = A(t), ww = A(w), hh = A(h);
      g.fillStyle = "rgba(0,0,0,.18)"; g.fillRect(x + 1, y + 2, ww, hh);
      R(x, y, ww, hh, shade(c, -22)); R(x + 2, y + 2, ww - 4, hh - 4, c); R(x + 4, y + 4, ww - 8, hh - 8, shade(c, 10));
      R(x + 6, y + 6, ww - 12, 1, shade(c, -12)); R(x + 6, y + hh - 7, ww - 12, 1, shade(c, -12)); R(x + 6, y + 6, 1, hh - 12, shade(c, -12)); R(x + ww - 7, y + 6, 1, hh - 12, shade(c, -12));
      for (let i = x + 10; i < x + ww - 10; i += 6) { R(i, y + 3, 2, 1, shade(c, 26)); R(i, y + hh - 4, 2, 1, shade(c, 26)); }
    }
    for (const a of Object.values(AGENTS)) {
      const x = A(a.x), y = A(a.y);
      g.fillStyle = "rgba(0,0,0,.22)"; g.fillRect(x + 9, y - 10, 34, 4);
      R(x + 8, y - 22, 34, 12, "#5a3820"); R(x + 8, y - 22, 34, 3, "#80532e"); R(x + 8, y - 19, 34, 1, "#6a4426");
      R(x + 9, y - 10, 2, 5, "#432817"); R(x + 39, y - 10, 2, 5, "#432817");
      if (a.human) {
        R(x + 12, y - 21, 10, 7, "#efe9da"); R(x + 13, y - 20, 8, 1, "#b9b2a0"); R(x + 13, y - 18, 6, 1, "#b9b2a0");
        R(x + 31, y - 30, 2, 9, "#2e3138"); R(x + 28, y - 31, 8, 3, "#e8c66a"); R(x + 25, y - 21, 8, 2, "#2e3138");
        continue;
      }
      R(x + 19, y - 24, 4, 3, "#2a2d34");
      R(x + 13, y - 34, 16, 11, "#22252c"); R(x + 13, y - 34, 16, 1, "#3a3e47");
      R(x + 23, y - 20, 12, 3, "#c9c5ba"); R(x + 23, y - 20, 12, 1, "#e2ded3");
      R(x + 36, y - 22, 3, 3, "#e9e2cf"); R(x + 39, y - 21, 1, 1, "#e9e2cf");
      R(x + 10, y - 21, 7, 5, "#efe9da"); R(x + 11, y - 20, 5, 1, "#b9b2a0");
    }
    [[22, 50, 0], [W - 22, 50, 1], [22, 262, 2], [W - 22, 262, 0], [312, 120, 1], [312, 190, 2], [440, 120, 0], [402, 190, 1], [118, 118, 2], [150, 262, 1]].forEach(([x, y, k]) => plant(x, y, k));
    R(52, 228, 62, 34, "#4a2d18"); R(54, 230, 58, 30, "#5e3a20");
    for (let r = 0; r < 3; r++) { R(54, 239 + r * 10, 58, 1, "#3a2212"); for (let i = 0; i < 14; i++) { const h = 6 + (i * 7 % 3); R(56 + i * 4, 238 + r * 10 - h, 3, h, ["#3c5a85", "#b4532a", "#55703f", "#d9d4c7", "#6B4E71", "#c9a34a"][(i + r * 2) % 6]); } }
    R(330, 136, 30, 18, "#c9c5ba"); R(331, 137, 28, 16, "#f4f2ec"); [4, 8, 6, 11].forEach((h, i) => R(334 + i * 6, 151 - h, 4, h, ["#5d7fa6", "#c0602f", "#5c7a42", "#ffd66b"][i]));
    R(64, 182, 30, 9, "#7a3e3e"); R(64, 180, 30, 4, "#8e4b4b"); R(62, 181, 3, 10, "#6a3434"); R(93, 181, 3, 10, "#6a3434");
    R(104, 140, 8, 12, "#3a3d44"); R(105, 141, 6, 3, "#c94f3f"); R(106, 147, 4, 3, "#e9e2cf");
    R(140, 136, 6, 4, "#a6dcef"); R(139, 140, 8, 12, "#e9e6dd"); R(141, 143, 4, 1, "#7cb6d6");
    R(270, 208, 16, 9, "#d8d4ca"); R(270, 208, 16, 2, "#efebe2"); R(273, 210, 10, 1, "#555");
  }
  drawWorld();
  for (const [l, t, , h, label] of ROOMS) { const lb = el("div", "room-label", {left: (l + 16) + "px", top: (t + h - 26) + "px"}); lb.textContent = label; S.append(lb); }
  const shelfLabel = el("div", "room-label", {left: "156px", top: "664px"}); shelfLabel.textContent = "Documents"; S.append(shelfLabel);
  const shelf = el("div", "shelf", {left: "350px", top: "792px"}); S.append(shelf);

  // ================= live monitors =================
  const mon = el("canvas", "world"); mon.width = W; mon.height = H; S.append(mon);
  const m = mon.getContext("2d");
  let monT = 0;
  function drawMonitors() {
    m.clearRect(0, 0, W, H); if (!reducedMotion.matches) monT++;
    for (const [name, a] of Object.entries(AGENTS)) {
      if (a.human) continue;
      const x = A(a.x) + 14, y = A(a.y) - 33, w = 14, h = 9, n = nodes[name];
      const on = n && n.busy > 0;
      m.fillStyle = on ? shade(a.glow, -150) : "#16303c"; m.fillRect(x, y, w, h);
      if (on) {
        for (let r = 0; r < 4; r++) { const len = 3 + ((monT + r * 3 + name.length) * 7) % 9; m.fillStyle = r % 2 ? a.glow : shade(a.glow, -60); m.fillRect(x + 1 + (r % 2), y + 1 + r * 2, Math.min(len, w - 2), 1); }
        m.fillStyle = "rgba(255,255,255,.18)"; m.fillRect(x, y, w, 1);
        m.fillStyle = a.glow + "33"; m.fillRect(x - 3, y - 2, w + 6, h + 5);
      } else {
        m.fillStyle = shade(a.glow, -120); m.fillRect(x + 1, y + 1, 6, 1); m.fillRect(x + 1, y + 3, 9, 1); m.fillRect(x + 1, y + 5, 4, 1);
        if (monT % 4 < 2) { m.fillStyle = a.glow; m.fillRect(x + 6, y + 5, 1, 1); }
      }
    }
  }

  // ================= characters =================
  const FRONT = [
    ".......g........",
    ".......a........",
    ".....oooooo.....",
    "....ohhhhhho....",
    "...ohhHHhhhho...",
    "...ohhhhhhhho...",
    "...ohssssssho...",
    "...osssssssso...",
    "...ossessesso...",
    "...okssssssko...",
    "....ossSSsso....",
    ".....oossoo.....",
    "...occcllccco...",
    "..oCccccccccCo..",
    "..oCccccccccCo..",
    "..oCccccccccCo..",
    "..osCccccccCso..",
    "...oppppppppo...",
    "...opppPPpppo...",
    "...opppoopppo...",
    "...opppoopppo...",
    "...obbboobbbo...",
    "....ooo..ooo....",
  ];
  const LEGS = {
    0: FRONT.slice(19),
    1: ["...opppoopppo...", "...obbboopppo...", "....ooo.obbbo...", ".........ooo...."],
    2: ["...opppoopppo...", "...opppoobbbo...", "...obbbo.ooo....", "....ooo........."],
  };
  function drawSprite(c, look, frame, dir, blink, bob, typing) {
    const x = c.getContext("2d"); x.clearRect(0, 0, 16, 25);
    x.fillStyle = "rgba(0,0,0,.28)"; x.beginPath(); x.ellipse(8, 23.5, 5.5, 1.5, 0, 0, 7); x.fill();
    const rows = FRONT.slice(0, 19).concat(LEGS[frame] || LEGS[0]);
    const pal = {o: "#1d1a20", h: look.hair, H: shade(look.hair, 45), s: look.skin, S: shade(look.skin, -28), e: "#1b1b1f", k: shade(look.skin, -12),
      c: look.shirt, C: shade(look.shirt, -26), l: look.human ? "#efe9da" : shade(look.shirt, 34), p: look.pants, P: shade(look.pants, 18), b: "#15161a",
      a: "#3a3d44", g: look.glow || "#ffffff"};
    const ex = dir < 0 ? -1 : dir > 0 ? 1 : 0;
    const now = performance.now();
    for (let r = 0; r < rows.length; r++) {
      const yy = r - bob;
      for (let col = 0; col < 16; col++) {
        let ch = rows[r][col];
        if (ch === "." || ch === undefined) continue;
        if (look.human && (ch === "g" || ch === "a")) continue;
        if (ch === "e" && blink) ch = "s";
        let cx = col;
        if (r >= 6 && r <= 10 && (ch === "e" || ch === "k" || ch === "S")) cx = col + ex;
        if (r === 16 && ch === "s" && typing) { x.fillStyle = pal.s; x.fillRect(cx, yy - ((Math.floor(now / 120) + col) % 2), 1, 1); continue; }
        if (ch === "g") { x.fillStyle = (Math.floor(now / 600) % 2) ? pal.g : shade(pal.g, -80); x.fillRect(cx, yy, 1, 1); continue; }
        x.fillStyle = pal[ch] || ch; x.fillRect(cx, yy, 1, 1);
      }
    }
  }

  const nodes = {};
  function makeChar(name, look, x, y) {
    const n = el("div", "agent", {left: x + "px", top: y + "px", zIndex: String(Math.round(y))});
    const c = document.createElement("canvas"); c.width = 16; c.height = 25; n.append(c);
    const tag = el("div", "tag"); tag.append(document.createElement("i"), document.createElement("span"), document.createElement("small")); tag.children[1].textContent = name; n.append(tag);
    n.append(el("span", "carry"));
    S.append(n);
    return {el: n, canvas: c, look, x, y, home: {x, y}, log: [], busy: 0, dir: 0, moving: false, key: ""};
  }
  for (const [name, a] of Object.entries(AGENTS)) {
    const n = makeChar(name, a, a.x, a.y); nodes[name] = n;
    n.el.title = name; n.el.setAttribute("role", "button"); n.el.setAttribute("tabindex", "0"); n.el.setAttribute("aria-label", "Open " + name + " work sheet"); n.el.addEventListener("click", () => openSheet(name));
    n.el.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openSheet(name); } });
  }
  const visitor = makeChar("Visitor", {hair: "#5a4632", skin: "#d9a27a", shirt: "#8f8b80", pants: "#3b3a36", human: true}, 20, 482);
  visitor.el.style.opacity = 0; visitor.el.style.pointerEvents = "none";

  function tick(t) {
    if (reducedMotion.matches) t = 0;
    for (const n of [...Object.values(nodes), visitor]) {
      const frame = n.moving ? [1, 0, 2, 0][Math.floor(t / 130) % 4] : 0;
      const bob = n.moving ? (frame === 0 ? 1 : 0) : (Math.floor((t + n.home.x * 7) / 900) % 2);
      const blink = !n.moving && (Math.floor((t + n.home.x * 13) / 110) % 40 === 0);
      const typing = !n.moving && n.busy > 0;
      const glow = Math.floor(t / 600) % 2;
      const key = `${frame}${bob}${blink}${n.dir}${typing ? Math.floor(t / 120) % 2 : "x"}${glow}`;
      if (key !== n.key) { n.key = key; drawSprite(n.canvas, n.look, frame, n.dir, blink, bob, typing); }
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  setInterval(drawMonitors, 180);

  function fit() { const area = document.getElementById("stage-wrap"); const k = Math.min(area.clientWidth / 1600, area.clientHeight / 900); S.style.transform = `scale(${k})`; }
  addEventListener("resize", fit); fit();

  // ================= movement =================
  function moveTo(n, x, y, ms) {
    if (reducedMotion.matches) ms = 1;
    n.dir = Math.sign(Math.round(x - n.x)); n.moving = true;
    const from = {left: n.el.style.left, top: n.el.style.top};
    n.x = x; n.y = y; n.el.style.zIndex = String(Math.round(Math.max(y, parseFloat(from.top))));
    return n.el.animate([from, {left: x + "px", top: y + "px"}], {duration: ms, easing: "linear", fill: "forwards"}).finished
      .then(() => { n.el.style.left = x + "px"; n.el.style.top = y + "px"; n.el.style.zIndex = String(Math.round(y)); n.moving = false; n.dir = 0; });
  }
  const speed = (n, x, y) => Math.max(260, Math.hypot(x - n.x, y - n.y) * 2.2);
  async function walk(name, tx, ty) {
    const n = nodes[name] || name;
    if (Math.abs(n.y - ty) > 90) { const lane = (n.y + ty) / 2; await moveTo(n, n.x, lane, speed(n, n.x, lane)); await moveTo(n, tx, lane, speed(n, tx, lane)); }
    await moveTo(n, tx, ty, speed(n, tx, ty));
  }

  function chip(x, y, text, tone, ms = 1500) { const c = el("div", "chip " + (tone || ""), {left: x + "px", top: (y - 104) + "px"}); c.textContent = text; S.append(c); setTimeout(() => c.remove(), ms); }
  function bubble(x, y, text, ms = 4200) { const b = el("div", "bubble", {left: x + "px", top: (y - 100) + "px"}); b.textContent = text; S.append(b); setTimeout(() => { b.style.transition = "opacity .4s"; b.style.opacity = 0; setTimeout(() => b.remove(), 400); }, ms); }
  function setSub(name, text) { const n = nodes[name]; if (n) n.el.querySelector(".tag small").textContent = "· " + text; }
  function busy(name, on) { const n = nodes[name]; if (!n) return; n.busy = Math.max(0, n.busy + (on ? 1 : -1)); n.el.classList.toggle("busy", n.busy > 0); }
  function ticker(agent, text) { const t = document.getElementById("ticker"); t.replaceChildren(); const em = document.createElement("em"); em.textContent = agent; t.append(em, document.createTextNode(text)); }
  for (const [name, a] of Object.entries(AGENTS)) setSub(name, a.human ? "on call" : "ready");

  // ambient life: idle agents get up, stretch and wander their room
  setInterval(() => {
    if (reducedMotion.matches) return;
    for (const n of Object.values(nodes)) {
      if (n.busy || n.walking || n.moving || Math.random() < .6) continue;
      const tx = n.home.x - 30 + (Math.random() - .5) * 110, ty = n.home.y + 16 + Math.random() * 30;
      moveTo(n, tx, ty, speed(n, tx, ty)).then(() => setTimeout(() => { if (!n.walking && !n.busy) moveTo(n, n.home.x, n.home.y, speed(n, n.home.x, n.home.y)); }, 900 + Math.random() * 1800));
    }
  }, 2300);


  // ================= the case each agent is working on =================
  const TOUR = new URLSearchParams(location.search).has("tour");
  if (TOUR) document.body.classList.add("tour");
  const focusLoan = {};            // agent -> loan id it last touched
  let STATE = {loans: [], documents: []}, stateAt = 0;
  async function refreshState(force) {
    if (!force && Date.now() - stateAt < 700) return STATE;
    stateAt = Date.now(); try { STATE = await (await fetch("/api/state")).json(); } catch (e) {}
    return STATE;
  }
  const rupee = v => "₹" + Math.round(Number(v || 0)).toLocaleString("en-IN");
  const pct = v => (Number(v || 0) * 100).toFixed(1) + "%";
  const DOCNAME = k => String(k || "document").replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
  const textNode = (tag, cls, text) => { const result = el(tag, cls); result.textContent = String(text ?? "Not recorded"); return result; };
  function caseDOM(name, L) {
    const result = document.createDocumentFragment();
    if (!L) return result;
    const head = el("div", "loanhead"); head.append(textNode("b", "", `${L.id} · ${L.name}`), textNode("span", "", `${rupee(L.requested_amount)} · ${L.tenure_months} months`)); result.append(head);
    const r = L.risk || {}, c = L.credit || {}, u = L.servicing_escalation || L.underwriting || {};
    const kv = rows => { const dl = el("dl", "kv"); rows.forEach(([key, value]) => dl.append(textNode("dt", "", key), textNode("dd", "", value))); result.append(dl); };
    const check = (ok, text) => { const row = el("div", "check" + (ok ? "" : " no")); row.append(textNode("i", "", ok ? "✓" : "—"), document.createTextNode(text)); result.append(row); };
    const flag = (title, text) => { const box = el("div", "flag"); box.append(textNode("b", "", title), textNode("span", "", text)); result.append(box); };
    switch (name) {
      case "Front Desk": kv([["Applicant", L.name], ["Current stage", DOCNAME(L.status)], ["First specialist", "Intake"]]); break;
      case "Intake": {
        const has = key => (L.documents || []).includes(key);
        check(has("identity"), has("identity") ? "Identity document received" : "Identity document missing");
        check(has("bank_statement"), has("bank_statement") ? "Bank statement received" : "Bank statement missing");
        check(L.consent === true, L.consent ? "Consent recorded" : "Consent not recorded");
        kv([["Identity reference", L.pan], ["Date of birth", L.dob]]); break;
      }
      case "Risk":
        kv([["Risk band", r.band || "Not assessed"]]);
        (r.codes || []).forEach((code, index) => flag(code, (r.explanations || [])[index] || "See the risk report."));
        if (r.band === "unverified") flag("Verification incomplete", (r.missing_sources || []).join(", "));
        else if (r.band && !(r.codes || []).length) check(true, "No unusual findings in the recorded checks");
        break;
      case "Credit": {
        if (!L.credit) { result.append(textNode("p", "", "Affordability has not been calculated.")); break; }
        kv([["Monthly income", rupee(Math.min(L.statement_income, L.monthly_income))], ["Existing EMIs", rupee(L.existing_emi)], ["New EMI", rupee(c.emi)], ["Eligible up to", rupee(c.eligible_amount)]]);
        const label = el("div", "barlabel"); label.append(textNode("span", "", `FOIR ${pct(c.foir)}`), textNode("span", "", "Policy cap 50%"));
        const bar = el("div", "bar"), fill = el("i"), cap = el("b"); fill.style.width = Math.min(100, Number(c.foir || 0) * 100) + "%"; cap.style.left = "50%"; bar.append(fill, cap); result.append(label, bar); break;
      }
      case "Underwriter":
        kv([["Decision", DOCNAME(u.decision || "Not decided")], ["Policy rule", u.rule || "Not evaluated"], ["Policy version", u.policy_version || "Not evaluated"]]);
        if (u.approved_amount) kv([["Approved amount", rupee(u.approved_amount)]]);
        { const reasons = el("ul", "reasons"); (u.reasons || L.rejection_reasons || []).slice(0, 3).forEach(reason => reasons.append(textNode("li", "", reason))); result.append(reasons); } break;
      case "Compliance":
        kv([["Recorded tool events", (nodes.Compliance.log || []).filter(row => row.kind === "tool").length], ["Borrower consent", L.consent ? "Recorded" : "Not recorded"], ["KFS acceptance", L.kfs_accepted ? "Recorded" : "Pending"], ["Payout account", L.account_verified ? "Verified fixture" : "Not verified"]]);
        result.append(textNode("p", "", "Borrower contact is limited to 08:00–19:00 IST. Payout checks run before simulation.")); break;
      case "Reporter": {
        const docs = el("div", "docs"), files = STATE.documents.filter(d => d.loan_id === L.id).slice(-7);
        files.forEach(file => docs.append(textNode("div", "", DOCNAME(file.kind))));
        if (!files.length) docs.append(textNode("div", "", "No documents yet")); result.append(docs); break;
      }
      case "Disbursement":
        kv([["Approved amount", rupee(L.approved_amount || u.approved_amount)], ["Key Facts Statement", L.kfs_accepted ? "Accepted" : L.kfs_issued ? "Issued" : "Not issued"], ["Account holder", L.payout_name], ["Simulated reference", L.simulated_utr || "Not paid out"]]); break;
      case "Collections":
        kv((L.schedule || []).slice(0, 3).map((row, index) => [`EMI ${index + 1}${row.due_date ? " · " + row.due_date : ""}`, rupee(row.emi)]));
        kv([["Installments recorded", `${L.paid_installments || 0} of ${L.schedule?.length || L.tenure_months}`], ["Status", DOCNAME(L.status)]]); break;
      case "Risk head":
        kv([["Status", DOCNAME(L.status)], ["Policy rule", u.rule || "Not evaluated"], ["Requested amount", rupee(L.requested_amount)]]);
        if (L.approved_amount) kv([["Approved amount", rupee(L.approved_amount)]]);
        if (L.override_reason) flag("Reason on record", L.override_reason); break;
    }
    return result;
  }
  async function renderCase(name) {
    const wrap = document.getElementById("s-case-wrap"), box = document.getElementById("s-case");
    const id = focusLoan[name]; if (!id) { wrap.hidden = true; return; }
    await refreshState();
    if (openName !== name) return;
    const L = STATE.loans.find(loan => loan.id === id);
    wrap.hidden = !L; box.replaceChildren(caseDOM(name, L));
  }
  function touch(name, e) { const m = (e.loan_id || "") || ((e.text || "").match(/L-\d+/) || [])[0]; if (m && nodes[name]) focusLoan[name] = m; }
  async function tourShow(name) { if (!TOUR || !nodes[name]) return; if (openName !== name) openSheet(name); else renderCase(name); }

  // ================= real events, paced to follow =================
  const queue = []; let playing = false;
  function push(e) { queue.push(e); if (!playing) play(); }
  async function play() { playing = true; while (queue.length) { const e = queue.shift(); try { await handle(e); } catch (err) { console.error(err); } } playing = false; }
  function record(name, e, kind, text) { const n = nodes[name]; if (!n) return; const row = {t: new Date(e.timestamp || Date.now()), kind, text}; n.log.push(row); if (openName === name) appendRow(row, true); }
  const carry = (n, text) => { const c = n.el.querySelector(".carry"); c.textContent = text; n.el.classList.toggle("carrying", !!text); };
  async function goHome(n) { await walk(n, n.home.x, n.home.y); }
  async function settle(n) { n.walking = true; if (n.moving) await sleep(450); }

  async function handle(e) {
    const a = e.agent, text = e.text || "";
    if (nodes[a]) touch(a, e);
    if (openName && e.kind !== "status") renderCase(openName);
    if (e.kind === "status") {
      const mm = text.match(/^Received a request as (\w+)/);
      if (mm) {
        const who = mm[1] === "ops" ? "Operations" : mm[1] === "risk_head" ? "Risk head" : "Borrower";
        visitor.el.querySelector(".tag span").textContent = who; visitor.el.querySelector(".tag small").textContent = "· arriving";
        visitor.el.style.opacity = 1; visitor.x = 20; visitor.y = 482; visitor.el.style.left = "20px"; visitor.el.style.top = "482px";
        await moveTo(visitor, 130, 486, 900); visitor.el.querySelector(".tag small").textContent = "· at the desk";
        ticker("Front Desk", "New request from " + who.toLowerCase()); busy("Front Desk", true); setSub("Front Desk", "routing a request"); await sleep(500); busy("Front Desk", false);
      }
      if (/ complete$/.test(text) && nodes[a]) { busy(a, false); setSub(a, "done"); }
      return;
    }
    if (e.kind === "handoff") {
      const mm = text.match(/^(.+?) → (.+?): (L-\d+) handed over \((.+)\)/); if (!mm) return;
      const [, from, to, loan, why] = mm; if (!nodes[from] || !nodes[to]) return;
      const n = nodes[from], t = nodes[to];
      ticker(from + " → " + to, loan + " · " + why); record(from, e, "handoff", `Handed ${loan} to ${to}: ${why}`); record(to, e, "received", `Received ${loan} from ${from}`);
      await settle(n); carry(n, "" + loan); setSub(from, "taking " + loan + " to " + to);
      await walk(n, t.home.x - 52, t.home.y + 6);
      chip(t.home.x, t.home.y, "Here’s " + loan, "navy", 1300); await sleep(650);
      carry(n, ""); busy(to, true); setSub(to, "working on " + loan); focusLoan[to] = loan; focusLoan[from] = loan; await tourShow(to);
      goHome(n).then(() => { n.walking = false; setSub(from, "ready"); }); await sleep(300); return;
    }
    if (e.kind === "tool") {
      if (!nodes[a]) return; const name = text.split(":")[0]; busy(a, true);
      chip(nodes[a].x, nodes[a].y, text.length > 48 ? name : text, "", 1100); setSub(a, name.replace(/_/g, " ")); record(a, e, "tool", text);
      ticker(a, "runs " + text); if (openName === a) markTool(name); await sleep(a === "Compliance" || a === "Reporter" ? 340 : 620); return;
    }
    if (e.kind === "document") {
      const kind = (e.data && e.data.kind) || "document"; const n = nodes.Reporter; focusLoan.Reporter = e.loan_id || focusLoan.Reporter;
      record("Reporter", e, "document", text); ticker("Reporter", text.replace("Issued ", "issued ").replace(/_/g, " "));
      await settle(n); carry(n, "" + kind.replace(/_/g, " ").slice(0, 22));
      await walk(n, SHELF.x + 60, SHELF.y - 70);
      shelf.append(el("div", "doc " + (/letter|rejection|sanction/.test(kind) ? "letter" : "pdf")));
      carry(n, ""); goHome(n).then(() => n.walking = false); await sleep(200); return;
    }
    if (e.kind === "file") { if (/\.png$/i.test(text)) shelf.append(el("div", "doc png")); return; }
    if (e.kind === "decision") {
      const d = (e.data && e.data.decision) || "", rule = ((e.data && e.data.rule) || "").replace(/^Policy rule:\s*/, ""); const n = nodes.Underwriter;
      focusLoan.Underwriter = e.loan_id || focusLoan.Underwriter; await refreshState(true); await tourShow("Underwriter");
      busy("Underwriter", true); record("Underwriter", e, "decision", text); ticker("Underwriter", text.replace(/^underwriter:\s*/i, ""));
      const tone = d === "decline" ? "decline" : d === "escalate" ? "escalate" : "approve";
      const b = el("div", "badge " + tone, {left: n.home.x + "px", top: (n.home.y - 140) + "px"});
      b.textContent = (d === "decline" ? "Declined" : d === "escalate" ? "Escalated" : "Approved") + (rule ? " · " + rule : ""); S.append(b);
      setTimeout(() => { b.style.transition = "opacity .6s"; b.style.opacity = 0; setTimeout(() => b.remove(), 600); }, 5600);
      setSub("Underwriter", b.textContent.toLowerCase());
      if (d === "escalate") {
        await sleep(500); const r = nodes.Reporter, h = nodes["Risk head"];
        await settle(r); carry(r, "case + note"); await walk(r, h.home.x - 56, h.home.y + 6);
        focusLoan["Risk head"] = e.loan_id; chip(h.home.x, h.home.y, "Needs your call", "amber", 2000); await sleep(400); await tourShow("Risk head"); busy("Risk head", true); setSub("Risk head", "1 case waiting"); await sleep(900);
        carry(r, ""); goHome(r).then(() => r.walking = false);
      }
      await sleep(1300); busy("Underwriter", false); return;
    }
    if (e.kind === "progress") {
      const clean = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1"); record("Reporter", e, "update", clean); ticker("Reporter", clean);
      if (/@risk_team|@risk_head/.test(text)) {
        const r = nodes.Reporter, h = nodes["Risk head"]; await settle(r); carry(r, "FYI");
        await walk(r, h.home.x - 56, h.home.y + 6); chip(h.home.x, h.home.y, "FYI · no action needed", "", 1600); await sleep(900);
        carry(r, ""); goHome(r).then(() => r.walking = false);
      }
      return;
    }
    if (e.kind === "reply") {
      const fd = nodes["Front Desk"], t = text.split("\n")[0]; record("Front Desk", e, "reply", t); ticker("Lendix", t);
      bubble(fd.x + 60, fd.y, t.length > 150 ? t.slice(0, 147) + "…" : t, 4800); await sleep(1500);
      setTimeout(async () => { await moveTo(visitor, 20, 482, 900); visitor.el.style.opacity = 0; }, 3400);
      for (const k of Object.keys(nodes)) if (k !== "Risk head") { nodes[k].busy = 0; nodes[k].el.classList.remove("busy"); }
      return;
    }
    if (e.kind === "error") ticker(a || "Office", text);
  }

  // ================= the work sheet =================
  let openName = null;
  function openSheet(name) {
    openName = name; const a = AGENTS[name], n = nodes[name];
    const av = document.getElementById("s-av"); av.textContent = ""; av.style.background = shade(a.shirt, 40); av.style.borderRadius = a.human ? "16px" : "50%";
    const big = document.createElement("canvas"); big.width = 16; big.height = 25; big.className = "sheet-sprite"; drawSprite(big, a, 0, 0, false, 0, false); av.append(big);
    document.getElementById("s-name").textContent = name; document.getElementById("s-role").textContent = a.role;
    const last = n.log[n.log.length - 1];
    document.getElementById("s-now").textContent = last ? last.text : (a.human ? "On call. Only escalated cases reach this desk." : "Ready at its desk.");
    const tools = document.getElementById("s-tools"); tools.replaceChildren();
    a.tools.forEach(t => { const s = document.createElement("span"); s.dataset.t = t; s.textContent = t; tools.append(s); });
    const ew = document.getElementById("s-extra-wrap");
    if (name === "Underwriter" || name === "Risk head") {
      ew.hidden = false; document.getElementById("s-extra-h").textContent = name === "Underwriter" ? "Credit policy v1" : "What reaches this desk";
      const extra = document.getElementById("s-extra"); extra.replaceChildren();
      if (name === "Underwriter") POLICY.forEach(([title, text]) => { const row = document.createElement("p"); row.append(textNode("b", "", title + ". "), document.createTextNode(text)); extra.append(row); });
      else extra.textContent = "Loans above ₹2 lakh, policy exceptions and override requests. Every decision needs a typed reason on record.";
    } else ew.hidden = true;
    const log = document.getElementById("s-log"); log.replaceChildren(); n.log.slice(-40).forEach(r => appendRow(r, false));
    const sheet = document.getElementById("sheet"); sheet.inert = false; sheet.classList.add("open");
    document.getElementById("sheet-toggle").textContent = "Hide agent sheet"; document.getElementById("sheet-toggle").setAttribute("aria-expanded", "true");
    renderCase(name);
  }
  function appendRow(r, live) {
    const log = document.getElementById("s-log"); const row = el("div", "row " + r.kind);
    const t = el("span", "t"); t.textContent = r.t.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
    const body = document.createElement("span"); const k = el("span", "k"); k.textContent = r.kind; body.append(k, document.createTextNode(r.text));
    row.append(t, body); log.prepend(row); if (live) document.getElementById("s-now").textContent = r.text;
  }
  function markTool(name) { document.querySelectorAll("#s-tools span").forEach(s => s.classList.toggle("on", s.dataset.t === name)); }
  let lastSheet = "Underwriter";
  document.getElementById("s-close").onclick = () => { if (openName) lastSheet = openName; openName = null; const sheet = document.getElementById("sheet"); sheet.classList.remove("open"); sheet.inert = true; document.getElementById("sheet-toggle").textContent = "Show agent sheet"; document.getElementById("sheet-toggle").setAttribute("aria-expanded", "false"); };
  document.getElementById("sheet-toggle").addEventListener("click", () => { if (openName) document.getElementById("s-close").click(); else openSheet(lastSheet); });
  document.addEventListener("keydown", event => { if (event.key === "Escape" && openName) { document.getElementById("s-close").click(); document.getElementById("sheet-toggle").focus(); } });
  Object.keys(AGENTS).forEach(name => { const option = document.createElement("option"); option.value = name; option.textContent = name; document.getElementById("agent-picker").append(option); });
  document.getElementById("agent-picker").addEventListener("change", event => { if (event.target.value) openSheet(event.target.value); });
  window.lendixOpen = openSheet; window.lendixClose = () => document.getElementById("s-close").click();

  // ================= command dock =================
  const QUICK = [["Review L-1042", "review L-1042", "ops"], ["Add statement", "add bank statement L-1042", "ops"], ["Clean loan L-1043", "review L-1043", "ops"], ["Borderline L-1045", "review L-1045", "ops"], ["Hardship L-1044", "I lost my job and can't pay L-1044", "borrower"]];
  const q = document.getElementById("quick");
  QUICK.forEach(([label, msg, role]) => { const b = document.createElement("button"); b.type = "button"; b.textContent = label; b.onclick = () => send(msg, role); q.append(b); });
  async function send(message, role) {
    if (!message || message.length > 8000) return;
    try {
      try { localStorage.setItem("lendix.role", role); } catch (_) {}
      document.getElementById("role").value = role;
      const response = await fetch("/api/chat", {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify({message, role, conversation_id: "office"})});
      if (!response.ok) { const result = await response.json(); throw new Error(typeof result.detail === "string" ? result.detail : "The office could not accept the request."); }
    } catch (error) { ticker("Office", error.message || "The office is unavailable. Please retry."); }
  }
  try { const savedRole = localStorage.getItem("lendix.role"); if (["ops", "risk_head", "borrower"].includes(savedRole)) document.getElementById("role").value = savedRole; } catch (_) {}
  document.getElementById("role").addEventListener("change", event => { try { localStorage.setItem("lendix.role", event.target.value); } catch (_) {} });
  document.getElementById("dock").onsubmit = ev => { ev.preventDefault(); const mm = document.getElementById("msg"); if (!mm.value.trim()) return; send(mm.value.trim(), document.getElementById("role").value); mm.value = ""; };
  window.lendixSend = send;
  window.lendixIdle = () => !playing && queue.length === 0;

  let src;
  function connectEvents() {
    src = new EventSource("/api/events");
    src.onmessage = ev => { try { push(JSON.parse(ev.data)); } catch (_) { ticker("Office", "An update could not be read. The live record will reconnect."); } };
    src.onerror = () => { document.getElementById("office-connection").textContent = "Reconnecting"; };
    src.onopen = () => { document.getElementById("office-connection").textContent = "Live events"; refreshState(true).then(record => { document.getElementById("office-connection").textContent = record.nonilion_connected ? "Live events · Nonilion connected" : "Live events"; }); };
  }
  connectEvents();
  addEventListener("pagehide", () => src.close());
  addEventListener("pageshow", event => { if (event.persisted) connectEvents(); });
})();
