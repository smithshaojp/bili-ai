const $ = (s) => document.querySelector(s);
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
};

let current = null;   // 当前视频完整数据
let pollTimer = null;
let chatting = false;

// ---------------- 工具 ----------------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTs = (sec) => {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
};
const tsToSec = (t) => t.split(":").map(Number).reduce((a, b) => a * 60 + b, 0);

// 行内格式：先转义，再处理代码/粗体/斜体/链接/时间戳
function inline(s) {
  const codes = [];
  s = esc(s).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
       .replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<em>$2</em>")
       .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
       .replace(/[\[【(（]?\b((?:\d{1,2}:)?\d{1,2}:\d{2})\b[\]】)）]?/g, (m, t) => `<button class="ts" data-t="${tsToSec(t)}">${m.includes("[") || m.includes("【") ? "[" + t + "]" : t}</button>`);
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => {
    const c = codes[+i];
    const m = c.match(/^\[?((?:\d{1,2}:)?\d{1,2}:\d{2})\]?$/);
    return m ? `<button class="ts" data-t="${tsToSec(m[1])}">[${m[1]}]</button>` : `<code>${c}</code>`;
  });
}

// 简易 Markdown 渲染（够用：标题、列表、引用、代码块、段落）
function md(src) {
  const lines = String(src || "").replace(/\r/g, "").split("\n");
  let html = "", para = [], stack = [], inCode = false, code = [];
  const flushPara = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
  const closeLists = (depth = 0) => { while (stack.length > depth) html += `</li></${stack.pop().type}>`; };
  for (const raw of lines) {
    if (inCode) {
      if (/^\s*```/.test(raw)) { html += `<pre><code>${esc(code.join("\n"))}</code></pre>`; inCode = false; code = []; }
      else code.push(raw);
      continue;
    }
    if (/^\s*```/.test(raw)) { flushPara(); closeLists(); inCode = true; continue; }
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { flushPara(); continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flushPara(); closeLists(); const lv = Math.min(m[1].length + 1, 4); html += `<h${lv}>${inline(m[2])}</h${lv}>`; continue; }
    if ((m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
      flushPara();
      const depth = Math.floor(m[1].replace(/\t/g, "  ").length / 2) + 1;
      const type = /\d/.test(m[2]) ? "ol" : "ul";
      if (stack.length > depth) closeLists(depth);
      if (stack.length === depth && stack[depth - 1].type !== type) closeLists(depth - 1);
      if (stack.length < depth) { while (stack.length < depth) { html += `<${type}><li>`; stack.push({ type }); } }
      else html += `</li><li>`;
      html += inline(m[3]);
      continue;
    }
    if ((m = line.match(/^>\s?(.*)$/))) { flushPara(); closeLists(); html += `<blockquote>${inline(m[1])}</blockquote>`; continue; }
    if (/^\s*(---|\*\*\*)\s*$/.test(line)) { flushPara(); closeLists(); html += "<hr>"; continue; }
    if (stack.length && /^\s+/.test(raw)) { html += " " + inline(line.trim()); continue; }
    closeLists();
    para.push(line.trim());
  }
  if (inCode) html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
  flushPara(); closeLists();
  return html;
}

// ---------------- 播放器 ----------------
function setPlayer(t = 0, autoplay = false) {
  if (!current) return;
  const q = new URLSearchParams({ bvid: current.bvid, p: current.page || 1, t: Math.floor(t), autoplay: autoplay ? 1 : 0, danmaku: 0, high_quality: 1 });
  $("#player").src = `https://player.bilibili.com/player.html?${q}`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest(".ts");
  if (!b) return;
  e.preventDefault();
  setPlayer(+b.dataset.t, true);
  if (window.innerWidth <= 1100) $(".player-wrap").scrollIntoView({ behavior: "smooth" });
});

// ---------------- 历史 ----------------
async function loadHistory() {
  const items = await api("/api/videos");
  $("#history").innerHTML = items.length ? items.map((v) => `
    <div class="hitem ${current && current.id === v.id ? "active" : ""}" data-id="${esc(v.id)}">
      ${v.pic ? `<img src="${esc(v.pic)}@160w_100h_1c.webp" referrerpolicy="no-referrer" loading="lazy" onerror="this.style.visibility='hidden'">` : `<img>`}
      <div><div class="t">${esc(v.title)}</div>
      <div class="s">${v.status === "done" ? fmtTs(v.duration) + (v.owner ? " · " + esc(v.owner) : "") : v.status === "error" ? "⚠ 出错" : "处理中…"}</div></div>
    </div>`).join("") : `<div class="side-title">还没有记录</div>`;
}
$("#history").addEventListener("click", (e) => {
  const it = e.target.closest(".hitem");
  if (it) openVideo(it.dataset.id);
});

// ---------------- 视频视图 ----------------
const SRC_NAME = { cc: "UP主字幕", ai: "B站AI字幕", asr: "本地转写" };

function renderMeta(v, first) {
  $("#empty").classList.add("hidden");
  $("#view").classList.remove("hidden");
  $("#vTitle").textContent = v.title || v.bvid;
  $("#vOwner").textContent = v.owner ? "UP主：" + v.owner : "";
  $("#vDur").textContent = v.duration ? "时长 " + fmtTs(v.duration) : "";
  $("#vSrc").textContent = v.source_type ? "文字稿：" + (SRC_NAME[v.source_type] || "") + (v.source_label ? `（${v.source_label}）` : "") : "";
  $("#vLink").href = `https://www.bilibili.com/video/${v.bvid}` + (v.page > 1 ? `?p=${v.page}` : "");
  $("#exportBtn").href = `/api/videos/${encodeURIComponent(v.id)}/export.md`;
  if (first) setPlayer(0, false);

  const busy = !["done", "error"].includes(v.status);
  $("#status").classList.toggle("hidden", !busy);
  $("#statusText").textContent = v.message || "处理中…";
  const hasBar = typeof v.progress === "number";
  $("#status .bar").classList.toggle("hidden", !hasBar);
  if (hasBar) $("#barFill").style.width = (v.progress * 100).toFixed(1) + "%";
  $("#errorBox").classList.toggle("hidden", v.status !== "error");
  $("#errorBox").textContent = v.status === "error" ? "出错了：" + v.message : "";
  $("#resumBtn").disabled = busy; $("#asrBtn").disabled = busy;

  $("#summary").innerHTML = v.summary ? md(v.summary) :
    `<p class="note">${v.status === "error" ? "没有生成总结。修复问题后可点「重新总结」或「本地重新转写」。" : "总结生成中…"}</p>`;
  const ready = (v.transcript && v.transcript.length) || v.transcript_len;
  $("#chatInput").disabled = !ready; $("#sendBtn").disabled = !ready || chatting;
  $("#chatInput").placeholder = ready ? "问点什么…（Enter 发送，Shift+Enter 换行）" : "等文字稿准备好后就能提问";
}

function renderTranscript(v) {
  const segs = v.transcript || [];
  if (!segs.length) { $("#transcript").innerHTML = `<p class="note">还没有文字稿</p>`; return; }
  // 合并成每 ~20 秒一行，便于阅读
  const rows = []; let buf = [], st = null;
  for (const s of segs) {
    if (st === null) st = s.s;
    buf.push(s.t);
    if (s.e - st >= 20) { rows.push([st, buf.join(" ")]); buf = []; st = null; }
  }
  if (buf.length) rows.push([st, buf.join(" ")]);
  $("#transcript").innerHTML = `<div class="src">${esc(SRC_NAME[v.source_type] || "")} · 共 ${segs.length} 句${v.subtitle_reason ? " · 未用B站字幕原因：" + esc(v.subtitle_reason) : ""}</div>` +
    rows.map(([t, txt]) => `<div class="line"><button class="ts" data-t="${Math.floor(t)}">${fmtTs(t)}</button><span>${esc(txt)}</span></div>`).join("");
}

const SUGGESTIONS = ["这个视频最重要的 3 个观点是什么？", "有哪些具体的数据、案例或方法？", "UP主的论证有没有漏洞或值得怀疑的地方？", "用大白话给我讲一遍", "我该怎么把这些内容用到实际中？"];

function renderChat(v) {
  const msgs = v.chat || [];
  $("#chatList").innerHTML = msgs.length ? msgs.map((m) =>
    m.role === "user" ? `<div class="msg user">${esc(m.content)}</div>` : `<div class="msg assistant md">${md(m.content)}</div>`
  ).join("") : `<div class="chat-empty">基于视频完整文字稿回答，回答里的时间戳可以点击跳转。</div>`;
  $("#suggest").innerHTML = msgs.length ? "" : SUGGESTIONS.map((s) => `<button type="button">${esc(s)}</button>`).join("");
  $("#chatList").scrollTop = $("#chatList").scrollHeight;
}
$("#suggest").addEventListener("click", (e) => {
  if (e.target.tagName === "BUTTON" && !$("#chatInput").disabled) { $("#chatInput").value = e.target.textContent; sendChat(); }
});

async function openVideo(id) {
  clearTimeout(pollTimer);
  const v = await api(`/api/videos/${encodeURIComponent(id)}`);
  const first = !current || current.id !== id;
  current = v;
  location.hash = id;
  renderMeta(v, first); renderTranscript(v); renderChat(v);
  loadHistory();
  if (!["done", "error"].includes(v.status)) poll();
}

async function poll() {
  clearTimeout(pollTimer);
  if (!current) return;
  const id = current.id;
  try {
    const v = await api(`/api/videos/${encodeURIComponent(id)}?light=1`);
    if (!current || current.id !== id) return;
    const hadTranscript = (current.transcript || []).length;
    Object.assign(current, v);
    if (!hadTranscript && v.transcript_len) {
      const full = await api(`/api/videos/${encodeURIComponent(id)}`);
      current.transcript = full.transcript; renderTranscript(current);
    }
    renderMeta(current, false);
    if (["done", "error"].includes(v.status)) { loadHistory(); return; }
  } catch (e) { console.warn(e); }
  pollTimer = setTimeout(poll, 1000);
}

// ---------------- 操作 ----------------
$("#addForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#urlInput").value.trim();
  if (!url) return;
  const btn = e.submitter || $("#addForm .primary");
  btn.disabled = true;
  try {
    const r = await api("/api/videos", { method: "POST", body: JSON.stringify({ url, force_asr: $("#forceAsr").checked }) });
    $("#urlInput").value = ""; $("#forceAsr").checked = false;
    await openVideo(r.id);
  } catch (err) { alert(err.message); }
  finally { btn.disabled = false; }
});

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
  $("#summary").classList.toggle("hidden", t.dataset.tab !== "summary");
  $("#transcript").classList.toggle("hidden", t.dataset.tab !== "transcript");
}));

$("#resumBtn").onclick = async () => { await api(`/api/videos/${encodeURIComponent(current.id)}/resummarize`, { method: "POST" }); current.summary = ""; poll(); };
$("#asrBtn").onclick = async () => {
  if (!confirm("下载音频并在本机重新转写？（没有字幕或字幕质量差时用，耗时较长）")) return;
  await api(`/api/videos/${encodeURIComponent(current.id)}/retry?force_asr=true`, { method: "POST" });
  current.transcript = []; poll();
};
$("#delBtn").onclick = async () => {
  if (!confirm("删除这条记录（总结、文字稿、对话）？")) return;
  await api(`/api/videos/${encodeURIComponent(current.id)}`, { method: "DELETE" });
  current = null; location.hash = "";
  $("#view").classList.add("hidden"); $("#empty").classList.remove("hidden"); $("#player").src = "about:blank";
  loadHistory();
};
$("#clearChat").onclick = async () => {
  if (!current || !confirm("清空这个视频的对话记录？")) return;
  await api(`/api/videos/${encodeURIComponent(current.id)}/chat/clear`, { method: "POST" });
  current.chat = []; renderChat(current);
};

async function sendChat() {
  const q = $("#chatInput").value.trim();
  if (!q || chatting || !current) return;
  chatting = true; $("#sendBtn").disabled = true; $("#chatInput").value = "";
  const id = current.id;
  current.chat = current.chat || [];
  if (!current.chat.length) { $("#chatList").innerHTML = ""; $("#suggest").innerHTML = ""; }
  $("#chatList").insertAdjacentHTML("beforeend", `<div class="msg user">${esc(q)}</div><div class="msg assistant md"><span class="note">思考中…</span></div>`);
  const box = $("#chatList").lastElementChild;
  $("#chatList").scrollTop = $("#chatList").scrollHeight;
  let out = "";
  try {
    const r = await fetch(`/api/videos/${encodeURIComponent(id)}/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: q }) });
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail; } catch {} throw new Error(m); }
    const reader = r.body.getReader(); const dec = new TextDecoder();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      out += dec.decode(value, { stream: true });
      box.innerHTML = md(out);
      const cl = $("#chatList");
      if (cl.scrollHeight - cl.scrollTop - cl.clientHeight < 120) cl.scrollTop = cl.scrollHeight;
    }
    if (current && current.id === id) current.chat.push({ role: "user", content: q }, { role: "assistant", content: out });
  } catch (err) {
    box.innerHTML = `<span class="danger">出错了：${esc(err.message)}</span>`;
  } finally {
    chatting = false; $("#sendBtn").disabled = false; $("#chatInput").focus();
  }
}
$("#chatForm").addEventListener("submit", (e) => { e.preventDefault(); sendChat(); });
$("#chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
});

// ---------------- 设置 ----------------
async function loadSettings() {
  const c = await api("/api/config");
  $("#keyState").textContent = c.deepseek_key_set ? `✓ 已设置（${c.deepseek_key_source}：${c.deepseek_key_mask}）` : "✗ 未设置";
  $("#keyState").className = "state " + (c.deepseek_key_set ? "ok" : "no");
  $("#sessState").textContent = c.sessdata_set ? `✓ 已设置（${c.sessdata_mask}）` : "未设置（没有它很多视频拿不到AI字幕）";
  $("#sessState").className = "state " + (c.sessdata_set ? "ok" : "no");
  $("#cfgModel").value = c.deepseek_model; $("#cfgBase").value = c.deepseek_base_url;
  $("#cfgEngine").value = c.asr_engine; $("#cfgAsrModel").value = c.asr_model;
  $("#cfgAsrModel").placeholder = "留空用默认：" + c.asr_default_model;
  $("#engState").textContent = "当前可用：" + c.asr_engine_active;
  $("#cfgMirror").checked = c.hf_mirror;
  $("#asrHint").textContent = c.asr_engine_active === "mlx"
    ? "Apple 芯片：默认 whisper-large-v3-turbo，1 小时视频约几分钟。首次会下载约 1.6GB 模型。"
    : "CPU 转写：默认 small 模型（约 460MB），1 小时视频大约 10~30 分钟。想更准可填 medium 或 large-v3-turbo（更慢）。";
  return c;
}
async function setupHint() {
  const c = await api("/api/config");
  const row = (ok, text) => `<div class="${ok ? "ok" : "no"}">${ok ? "✓" : "✗"} ${text}</div>`;
  $("#setupHint").innerHTML = (c.deepseek_key_set && c.sessdata_set) ? "" : `<div class="setup">
    ${row(c.deepseek_key_set, c.deepseek_key_set ? "DeepSeek Key 已就绪（" + c.deepseek_key_source + "）" : "还没有 DeepSeek Key（右上角设置里填，或设置环境变量 DEEPSEEK_API_KEY）")}
    ${row(c.sessdata_set, c.sessdata_set ? "B站 SESSDATA 已设置" : "建议在设置里填 B站 SESSDATA，否则很多视频拿不到 AI 字幕，只能本地转写（慢）")}
    ${row(c.asr_engine_active !== "未安装", "本地转写引擎：" + c.asr_engine_active)}
  </div>`;
}
$("#settingsBtn").onclick = async () => { await loadSettings(); $("#biliState").textContent = ""; $("#settings").showModal(); };
$("#saveSettings").onclick = async (e) => {
  e.preventDefault();
  const body = {
    deepseek_model: $("#cfgModel").value, deepseek_base_url: $("#cfgBase").value || "https://api.deepseek.com",
    asr_engine: $("#cfgEngine").value, asr_model: $("#cfgAsrModel").value, hf_mirror: $("#cfgMirror").checked,
  };
  if ($("#cfgKey").value.trim()) body.deepseek_api_key = $("#cfgKey").value.trim();
  if ($("#cfgSess").value.trim()) body.sessdata = $("#cfgSess").value.trim();
  try {
    await api("/api/config", { method: "POST", body: JSON.stringify(body) });
    $("#cfgKey").value = ""; $("#cfgSess").value = "";
    $("#settings").close(); setupHint();
  } catch (err) { alert(err.message); }
};
$("#clearKeys").onclick = async () => {
  if (!confirm("清除设置页里保存的 DeepSeek Key 和 SESSDATA？（环境变量里的不受影响）")) return;
  await api("/api/config", { method: "POST", body: JSON.stringify({ deepseek_api_key: "", sessdata: "" }) });
  await loadSettings(); setupHint();
};
$("#checkBili").onclick = async () => {
  $("#biliState").textContent = "检测中…"; $("#biliState").className = "state";
  try {
    const r = await api("/api/check-bili");
    $("#biliState").textContent = r.logged_in ? "✓ 已登录" : "✗ 未登录（SESSDATA 无效或未填写；先保存再检测）";
    $("#biliState").className = "state " + (r.logged_in ? "ok" : "no");
  } catch (err) { $("#biliState").textContent = "检测失败：" + err.message; }
};

// ---------------- 启动 ----------------
(async () => {
  await loadHistory();
  setupHint();
  const id = decodeURIComponent(location.hash.slice(1));
  if (id) openVideo(id).catch(() => {});
})();
