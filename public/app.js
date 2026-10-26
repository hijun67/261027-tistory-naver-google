"use strict";
/* 블로그 이전 통합 웹 버전 (네이버 / 티스토리 → 구글 블로거)
 * - 변환: Pyodide 웹 워커 (core_naver.py / core_tistory.py)
 * - 읽기: Netlify 프록시 함수 (naver.js / tistory.js)
 * - 발행: 브라우저가 구글 토큰(GIS)으로 Blogger API 를 직접 호출
 * 소스별로 설정·이전 기록을 따로 저장하고, 구글 로그인과 대상 블로거는 공유합니다.
 */
const $ = (id) => document.getElementById(id);
const SCOPE = "https://www.googleapis.com/auth/blogger";
const BLOGGER = "https://www.googleapis.com/blogger/v3";
const MAX_PER_RUN = 5; // 블로거의 글 작성 제한 때문에 한 번에 이전 권장 개수
const FIXED_CID = ((window.N2B_CONFIG && window.N2B_CONFIG.clientId) || "").trim(); // 운영자가 config.js 에 넣은 ID

const COMMON_DEFAULTS = {
  blogger_blog_id: "", blogger_blog_name: "", client_id: "",
  keep_original_date: true, publish_as_draft: false,
  delay_between_posts: 5, batch_count: 5,
  para_max_chars: 100, para_max_sentences: 3, pc_line_chars: 36, wrap_chars: 28,
  auto_heading: true, add_toc: true, add_json_ld: true, import_maps: true,
  hashtags_in_body: true, hashtags_as_labels: true, keep_original_paragraphs: false,
  image_width: 966, center_all: true,
};
const NUMS = [
  ["para_max_chars", "문단 최대 글자수"], ["para_max_sentences", "문단 최대 문장수"],
  ["pc_line_chars", "PC 한 줄 글자수"], ["wrap_chars", "의미단위 줄바꿈 기준(0=끔)"],
  ["image_width", "이미지 폭 px(0=원본)"], ["delay_between_posts", "글 사이 대기(초)"],
];

// ---------- 소스(네이버 / 티스토리) 정의 ----------
const SOURCES = {
  naver: {
    label: "네이버", blog: "네이버 블로그", tagWord: "해시태그",
    cfgKey: "n2b_cfg", stateKey: "n2b_state", idKey: "naver_blog_id", exportName: "migrated.json",
    defaults: Object.assign({}, COMMON_DEFAULTS, { naver_blog_id: "", labels: ["네이버블로그"], import_cover: true }),
    checks: [
      ["keep_original_date", "네이버 원본 작성일로 발행"], ["publish_as_draft", "초안으로 저장"],
      ["auto_heading", "굵은 줄을 소제목으로 변환"],
      ["add_toc", "목차 자동 삽입"], ["add_json_ld", "FAQ 구조화 데이터(JSON-LD)"],
      ["import_maps", "네이버 지도 → 구글 지도"], ["hashtags_in_body", "본문 하단에 #해시태그"],
      ["hashtags_as_labels", "해시태그를 블로거 라벨로"], ["keep_original_paragraphs", "원문 문단 그대로(재구성 안 함)"],
      ["import_cover", "대표(커버) 이미지 가져오기"], ["center_all", "전체 가운데 정렬"],
    ],
    placeholder: "예) myblog 또는 https://blog.naver.com/myblog",
    idLabel: "네이버 블로그 아이디 또는 주소",
    idError: "네이버 블로그 아이디 또는 주소를 확인해 주세요.",
    heroMain: "네이버 블로그<i>→</i>구글 블로거",
    heroAria: "ITNES.site 네이버 블로그 구글 블로거 이전, 아이디 입력·구글 로그인·글 선택 세 단계",
    heroSub: "아이디 입력 · 구글 로그인 · 글 선택, 세 단계로 한 번에 이전",
    h1: "네이버 블로그 <span class=\"arr\">→</span> 구글 블로거 이전",
    title: "네이버 블로그 → 구글 블로거 이전",
    intro: "네이버 블로그에 쓴 글을 <strong>내 구글 블로거 블로그로 옮겨 주는 도구</strong>입니다. 네이버 블로그 아이디를 넣고 구글 로그인만 하면, 글을 선택해서 한 번에 이전할 수 있습니다.",
    feat1: "글 문단 정리, 소제목·목차, 이미지, 해시태그(라벨), 지도 변환",
    feat2: "네이버 원본 작성일 유지 / 초안 저장 선택 가능",
    emptyList: "먼저 네이버 글 목록을 불러오세요.",
    startLog: "준비되었습니다. ① 네이버 글 목록을 불러오세요.",
    extractId(text) {
      text = (text || "").trim(); if (!text) return null;
      let m = /blogId=([A-Za-z0-9_\-]+)/.exec(text); if (m) return m[1];
      m = /naver\.com\/([A-Za-z0-9_\-]+)/.exec(text); if (m) return m[1];
      return /^[A-Za-z0-9_\-]+$/.test(text) ? text : null;
    },
    toPosts: (list) => list.map((p) => ({ key: String(p.logNo), sid: String(p.logNo), title: p.title || "", addDate: p.addDate || "" })),
    titleArg: (p) => p.title || `(제목 없음 ${p.key})`,
    titleOf: (p) => p.title || `(제목 없음 ${p.key})`,
    emptyResult: "글을 찾지 못했습니다. 아이디를 확인하고, 공개 글이 있는지 확인해 주세요.",
  },
  tistory: {
    label: "티스토리", blog: "티스토리 블로그", tagWord: "태그",
    cfgKey: "t2b_cfg", stateKey: "t2b_state", idKey: "tistory_blog", exportName: "migrated_tistory.json",
    defaults: Object.assign({}, COMMON_DEFAULTS, { tistory_blog: "", labels: ["티스토리"], keep_videos: true, check_images: true }),
    checks: [
      ["keep_original_date", "티스토리 원본 작성일로 발행"], ["publish_as_draft", "초안으로 저장"],
      ["auto_heading", "굵은 줄을 소제목으로 변환"],
      ["add_toc", "목차 자동 삽입"], ["add_json_ld", "FAQ 구조화 데이터(JSON-LD)"],
      ["import_maps", "지도 → 구글 지도"], ["hashtags_in_body", "본문 하단에 #해시태그"],
      ["hashtags_as_labels", "해시태그를 블로거 라벨로"], ["keep_original_paragraphs", "원문 문단 그대로(재구성 안 함)"],
      ["keep_videos", "동영상은 링크 카드로 남기기"], ["center_all", "전체 가운데 정렬"],
      ["check_images", "이미지 주소 점검 · 자동 대체 (깨진 이미지 방지)"],
    ],
    placeholder: "예) myblog 또는 https://myblog.tistory.com",
    idLabel: "티스토리 블로그 이름 또는 주소",
    idError: "티스토리 블로그 이름 또는 주소를 확인해 주세요. (예: myblog 또는 https://myblog.tistory.com)",
    heroMain: "티스토리<i>→</i>구글 블로거",
    heroAria: "티스토리 블로그 구글 블로거 이전, 블로그 이름 입력·구글 로그인·글 선택 세 단계",
    heroSub: "블로그 이름 입력 · 구글 로그인 · 글 선택, 세 단계로 한 번에 이전",
    h1: "티스토리 <span class=\"arr\">→</span> 구글 블로거 이전",
    title: "티스토리 블로그 → 구글 블로거 이전",
    intro: "티스토리에 쓴 글을 <strong>내 구글 블로거 블로그로 옮겨 주는 도구</strong>입니다. 티스토리 블로그 이름을 넣고 구글 로그인만 하면, 글을 선택해서 한 번에 이전할 수 있습니다.",
    feat1: "글 문단 정리, 소제목·목차, 이미지, 태그(라벨), 링크 카드·동영상 변환",
    feat2: "티스토리 원본 작성일 유지 / 초안 저장 선택 가능",
    emptyList: "먼저 티스토리 글 목록을 불러오세요.",
    startLog: "준비되었습니다. ① 티스토리 글 목록을 불러오세요.",
    extractId(text) {
      text = (text || "").trim(); if (!text) return null;
      const m = /([A-Za-z0-9][A-Za-z0-9\-]*)\.tistory\.com/i.exec(text); if (m) return m[1].toLowerCase();
      return /^[A-Za-z0-9][A-Za-z0-9\-]*$/.test(text) ? text.toLowerCase() : null;
    },
    // 티스토리는 블로그가 달라도 글 번호가 겹칠 수 있어 '블로그/글키' 로 기록을 구분
    toPosts: (list, id) => list.map((p) => Object.assign({}, p, { key: String(p.key), sid: id + "/" + p.key })),
    titleArg: (p) => p.title || "",
    titleOf: (p) => p.title || `(제목은 이전할 때 확인 · 글 ${String(p.key).replace(/^entry\//, "")})`,
    emptyResult: "글을 찾지 못했습니다. 블로그 이름을 확인하고, 공개 글이 있는지 확인해 주세요.",
  },
};

// ---------- 저장소 ----------
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { logLine("W", "브라우저 저장 실패: " + e.message); } },
};
const cfgs = {}, states = {}, views = {};
for (const k of Object.keys(SOURCES)) {
  const S = SOURCES[k];
  cfgs[k] = Object.assign({}, S.defaults, store.get(S.cfgKey, {}));
  states[k] = store.get(S.stateKey, { done: {}, failed: {} });
  states[k].done = states[k].done || {}; states[k].failed = states[k].failed || {};
  views[k] = { posts: [], postBySid: {}, status: ["", ""] };
}
// 대상 블로거는 두 소스가 공유: 어느 한쪽에 저장된 선택이 있으면 양쪽에 적용
{
  const any = Object.values(cfgs).find((c) => c.blogger_blog_id);
  if (any) for (const c of Object.values(cfgs)) { c.blogger_blog_id = any.blogger_blog_id; c.blogger_blog_name = any.blogger_blog_name; }
}

let src = "naver", S = SOURCES.naver, cfg = cfgs.naver, state = states.naver;
let posts = [], postBySid = {}; const rowMap = new Map();
const saveCfg = () => store.set(S.cfgKey, cfg);
const saveState = () => store.set(S.stateKey, state);

// ---------- 로그 ----------
function logLine(level, text) {
  const box = $("log");
  const d = document.createElement("div");
  d.className = level;
  d.textContent = new Date().toLocaleTimeString("ko-KR", { hour12: false }) + "  " + text;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}
const pyLogs = (logs) => (logs || []).forEach((l) => { const i = l.indexOf("|"); logLine(l.slice(0, i), l.slice(i + 1)); });
const setStatus = (id, text, cls) => { const e = $(id); e.textContent = text; e.className = "status " + (cls || ""); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- 파이썬 워커 RPC ----------
const worker = new Worker("worker.js");
let rpcId = 0; const pending = new Map();
worker.onmessage = (ev) => {
  const m = ev.data; const p = pending.get(m.id); if (!p) return;
  pending.delete(m.id); pyLogs(m.logs);
  m.ok ? p.resolve(m.result) : p.reject(new Error(m.error));
};
worker.onerror = (e) => logLine("E", "워커 오류: " + (e.message || e));
const rpc = (type, args) => new Promise((resolve, reject) => {
  const id = ++rpcId; pending.set(id, { resolve, reject }); worker.postMessage({ id, type, args: Object.assign({ src }, args) });
});
const pyReady = {};
async function ensurePy() {
  if (pyReady[src]) return;
  logLine("I", "변환 엔진을 준비하는 중입니다 (처음 한 번만, 약 10~30초)…");
  await rpc("init", {}); pyReady[src] = true; logLine("I", "변환 엔진 준비 완료");
}

// ---------- 화면 문구 / 소스 전환 ----------
function applyTexts() {
  document.title = S.title;
  $("logoSmall").textContent = `(${S.label} → 구글 블로거)`;
  $("heroMain").innerHTML = S.heroMain;
  $("heroSub").textContent = S.heroSub;
  $("heroTable").setAttribute("aria-label", S.heroAria);
  $("h1Title").innerHTML = S.h1;
  $("step1Title").textContent = S.blog;
  $("idLabel").textContent = S.idLabel;
  $("blogId").placeholder = S.placeholder;
  $("introText").innerHTML = S.intro;
  $("feat1").textContent = S.feat1;
  $("feat2").textContent = S.feat2;
  document.querySelectorAll("[data-src-only]").forEach((el) => { el.hidden = el.dataset.srcOnly !== src; });
  document.querySelectorAll(".tab").forEach((b) => {
    const on = b.dataset.src === src; b.classList.toggle("on", on); b.setAttribute("aria-selected", on ? "true" : "false");
  });
}
function switchSource(next) {
  if (next === src || !SOURCES[next]) return;
  if (running || busy) return alert("작업이 진행 중입니다. 끝난 뒤에 전환해 주세요.");
  views[src] = { posts, postBySid, status: [$("listStatus").textContent, $("listStatus").className.replace("status", "").trim()] };
  src = next; S = SOURCES[src]; cfg = cfgs[src]; state = states[src];
  posts = views[src].posts; postBySid = views[src].postBySid;
  store.set("mb_src", src);
  try { const u = new URL(location.href); u.searchParams.set("src", src); history.replaceState(null, "", u); } catch {}
  applyTexts(); buildOptions();
  $("blogId").value = cfg[S.idKey] || "";
  setStatus("listStatus", views[src].status[0], views[src].status[1]);
  $("countN").value = Math.min(MAX_PER_RUN, cfg.batch_count || MAX_PER_RUN);
  renderTable();
  logLine("I", `${S.label} → 구글 블로거 모드로 전환했습니다.`);
}
document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => switchSource(b.dataset.src)));

// ---------- 글 목록 ----------
$("btnList").onclick = async () => {
  const id = S.extractId($("blogId").value);
  if (!id) return setStatus("listStatus", S.idError, "bad");
  cfg[S.idKey] = id; saveCfg();
  setBusy(true);
  try {
    setStatus("listStatus", "변환 엔진 준비 및 글 목록 수집 중… (글이 많으면 오래 걸립니다)", "run");
    await ensurePy();
    const list = await rpc("list", { id });
    posts = S.toPosts(list, id);
    postBySid = Object.fromEntries(posts.map((p) => [p.sid, p]));
    renderTable();
    if (!posts.length) setStatus("listStatus", S.emptyResult, "bad");
    else setStatus("listStatus", `${id} · 글 ${posts.length}개를 불러왔습니다.`, "ok");
    logLine("I", `${S.label} 글 목록 ${posts.length}개`);
  } catch (e) {
    setStatus("listStatus", "목록을 불러오지 못했습니다: " + e.message, "bad"); logLine("E", e.message);
  } finally { setBusy(false); }
};

// ---------- 구글 로그인 / 블로거 ----------
let tokenClient = null, accessToken = null, tokenExp = 0;
function requestToken(prompt) {
  return new Promise((resolve, reject) => {
    const cid = FIXED_CID;
    if (!cid) return reject(new Error("서비스 설정이 완료되지 않았습니다. 운영자에게 문의해 주세요. (config.js 에 클라이언트 ID 필요)"));
    if (!window.google || !google.accounts) return reject(new Error("구글 로그인 스크립트를 불러오지 못했습니다. 새로고침 후 다시 시도해 주세요."));
    tokenClient = google.accounts.oauth2.initTokenClient({
      client_id: cid, scope: SCOPE,
      callback: (r) => {
        if (r.error) return reject(new Error("구글 로그인 실패: " + (r.error_description || r.error)));
        accessToken = r.access_token; tokenExp = Date.now() + (Number(r.expires_in || 3600) - 120) * 1000; resolve(accessToken);
      },
      error_callback: (e) => reject(new Error("구글 로그인 창이 닫혔거나 차단되었습니다: " + (e.type || ""))),
    });
    tokenClient.requestAccessToken({ prompt: prompt || "" });
  });
}
const ensureToken = async () => (accessToken && Date.now() < tokenExp ? accessToken : requestToken(accessToken ? "" : "consent"));

async function bg(path, opts = {}, retry = true) {
  const tok = await ensureToken();
  const res = await fetch(BLOGGER + path, Object.assign({}, opts, {
    headers: Object.assign({ Authorization: "Bearer " + tok, "Content-Type": "application/json" }, opts.headers || {}),
  }));
  if (res.status === 401 && retry) { accessToken = null; return bg(path, opts, false); }
  const text = await res.text(); let data = {}; try { data = JSON.parse(text); } catch {}
  if (!res.ok) { const err = new Error((data.error && data.error.message) || ("HTTP " + res.status)); err.status = res.status; throw err; }
  return data;
}

$("btnLogin").onclick = async () => {
  setBusy(true);
  try {
    await requestToken("consent");
    const data = await bg("/users/self/blogs");
    const sel = $("blogPick"); sel.innerHTML = "";
    const items = data.items || [];
    if (!items.length) { sel.innerHTML = '<option value="">블로그가 없습니다</option>'; setStatus("bloggerStatus", "이 계정에 블로거 블로그가 없습니다.", "bad"); return; }
    items.forEach((b) => { const o = document.createElement("option"); o.value = b.id; o.textContent = `${b.name} (${b.url})`; o.dataset.name = b.name; sel.appendChild(o); });
    if (cfg.blogger_blog_id && items.some((b) => b.id === cfg.blogger_blog_id)) sel.value = cfg.blogger_blog_id;
    applyBlog({ id: sel.value, name: sel.selectedOptions[0].dataset.name });
    logLine("I", `구글 로그인 완료 · 블로그 ${items.length}개`);
  } catch (e) { setStatus("bloggerStatus", e.message, "bad"); logLine("E", e.message); }
  finally { setBusy(false); }
};
$("blogPick").onchange = () => { const o = $("blogPick").selectedOptions[0]; if (o && o.value) applyBlog({ id: o.value, name: o.dataset.name }); };
function applyBlog(b) {
  for (const k of Object.keys(SOURCES)) { cfgs[k].blogger_blog_id = b.id; cfgs[k].blogger_blog_name = b.name; store.set(SOURCES[k].cfgKey, cfgs[k]); }
  setStatus("bloggerStatus", `선택됨: ${b.name} (ID ${b.id})`, "ok");
}

// ---------- 옵션 UI ----------
function buildOptions() {
  const box = $("opts"); box.innerHTML = "";
  S.checks.forEach(([k, t]) => {
    const l = document.createElement("label"); l.className = "chk";
    l.innerHTML = `<input type="checkbox" data-k="${k}"> ${t}`; box.appendChild(l);
    const i = l.querySelector("input"); i.checked = !!cfg[k]; i.onchange = () => { cfg[k] = i.checked; saveCfg(); };
  });
  NUMS.forEach(([k, t]) => {
    const l = document.createElement("div"); l.className = "num";
    l.innerHTML = `<input type="number" min="0" data-k="${k}"> ${t}`; box.appendChild(l);
    const i = l.querySelector("input"); i.value = cfg[k]; i.onchange = () => { cfg[k] = Math.max(0, Number(i.value) || 0); saveCfg(); };
  });
  $("optLabels").value = (cfg.labels || []).join(", ");
  $("optLabels").onchange = () => { cfg.labels = $("optLabels").value.split(",").map((s) => s.trim()).filter(Boolean); saveCfg(); };
}

// 옵션 영역 보이기/숨기기 (기본: 보임, 선택은 브라우저에 기억)
function setOptOpen(open) {
  $("optBody").hidden = !open;
  const b = $("btnOptToggle"); b.textContent = open ? "옵션 숨기기" : "옵션 보이기"; b.setAttribute("aria-expanded", open ? "true" : "false");
  store.set("mb_opt_open", open);
}
$("btnOptToggle").onclick = () => setOptOpen($("optBody").hidden);

// ---------- 테이블 ----------
const isDone = (sid) => !!state.done[sid];
function renderTable() {
  const tb = $("tbody"); tb.innerHTML = ""; rowMap.clear();
  if (!posts.length) {
    const tr = document.createElement("tr"); const td = document.createElement("td");
    td.colSpan = 4; td.style.color = "var(--sub)"; td.textContent = S.emptyList; tr.appendChild(td); tb.appendChild(tr);
  }
  posts.forEach((p) => {
    const tr = document.createElement("tr"); tr.dataset.sid = p.sid;
    tr.innerHTML = `<td><input type="checkbox" class="rowchk"></td><td class="d"></td><td class="t"></td><td class="s"></td>`;
    tr.children[1].textContent = p.addDate || "-"; tr.children[2].textContent = S.titleOf(p);
    tb.appendChild(tr); rowMap.set(p.sid, tr); paintRow(p.sid);
    tr.querySelector(".rowchk").onchange = updateSummary;
  });
  updateSummary();
}
function paintRow(sid, text, cls) {
  const tr = rowMap.get(sid); if (!tr) return; const s = tr.querySelector(".s");
  tr.classList.toggle("done", isDone(sid));
  if (text) { s.textContent = text; s.className = "s " + (cls || ""); return; }
  if (isDone(sid)) { s.textContent = "완료"; s.className = "s ok"; }
  else if (state.failed[sid]) { s.textContent = "실패(재시도 가능)"; s.className = "s bad"; }
  else { s.textContent = "대기"; s.className = "s"; }
}
const checks = () => [...$("tbody").querySelectorAll(".rowchk")];
const sidOfCheck = (c) => c.closest("tr").dataset.sid;
function updateSummary() {
  const total = posts.length, done = posts.filter((p) => isDone(p.sid)).length, sel = checks().filter((c) => c.checked).length;
  $("summary").textContent = `전체 ${total}개 · 이전 완료 ${done}개 · 선택 ${sel}개`;
}
$("btnFirstN").onclick = () => {
  const n = Math.min(MAX_PER_RUN, Math.max(1, Number($("countN").value) || MAX_PER_RUN)); $("countN").value = n; cfg.batch_count = n; saveCfg(); let c = 0;
  checks().forEach((chk) => { if (!isDone(sidOfCheck(chk)) && c < n) { chk.checked = true; c++; } else chk.checked = false; });
  updateSummary();
};
$("btnAll").onclick = () => { checks().forEach((c) => { c.checked = !isDone(sidOfCheck(c)); }); updateSummary(); };
$("btnNone").onclick = () => { checks().forEach((c) => (c.checked = false)); $("chkHead").checked = false; updateSummary(); };
$("chkHead").onchange = (e) => { e.target.checked ? $("btnAll").click() : $("btnNone").click(); };

// ---------- 이미지 점검 · 대체 ----------
// 변환 결과의 <img> 가 실제로 열리는지 브라우저에서 확인하고, 안 열리면 data-fb 의 대체 주소를 차례로 시도합니다.
const unescAttr = (t) => t.replace(/&quot;/g, '"').replace(/&#x27;|&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const escAttr = (t) => t.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
function probeImage(url, ms) {
  return new Promise((resolve) => {
    const im = new Image(); let done = false;
    const fin = (ok) => { if (done) return; done = true; clearTimeout(t); im.onload = im.onerror = null; im.src = ""; resolve(ok); };
    const t = setTimeout(() => fin(false), ms || 20000);
    im.referrerPolicy = "no-referrer"; // 블로거에서 보일 때와 같은 조건
    im.onload = () => fin(im.naturalWidth > 1 && im.naturalHeight > 1);
    im.onerror = () => fin(false);
    im.src = url;
  });
}
async function fixImages(content) {
  const tags = content.match(/<img\b[^>]*>/g) || [];
  const results = new Array(tags.length);
  let next = 0, swapped = 0, failed = 0;
  const run = async () => {
    while (next < tags.length) {
      const i = next++; const tag = tags[i];
      const sm = /\ssrc="([^"]*)"/.exec(tag); const fm = /\sdata-fb="([^"]*)"/.exec(tag);
      const urls = [];
      if (sm) urls.push(unescAttr(sm[1]));
      if (fm) unescAttr(fm[1]).split(/\s+/).filter(Boolean).forEach((u) => urls.push(u));
      let used = null, idx = -1;
      for (let k = 0; k < urls.length && !stopFlag; k++) { if (await probeImage(urls[k])) { used = urls[k]; idx = k; break; } }
      results[i] = { used, idx, first: urls[0] || "" };
    }
  };
  await Promise.all([run(), run(), run()]);
  let n = 0;
  const out = content.replace(/<img\b[^>]*>/g, (tag) => {
    const r = results[n++]; let t = tag.replace(/\sdata-fb="[^"]*"/, "");
    if (!r) return t;
    if (r.used && r.idx > 0) { t = t.replace(/\ssrc="[^"]*"/, ' src="' + escAttr(r.used) + '"'); swapped++; }
    else if (!r.used) { failed++; logLine("W", `  이미지 열리지 않음 (${n}번째): ${r.first.slice(0, 110)}`); }
    return t;
  });
  logLine(failed ? "W" : "I", `  이미지 점검: ${tags.length}개 · 대체 주소로 교체 ${swapped}개 · 열리지 않음 ${failed}개`);
  return out;
}

// ---------- 발행 ----------
async function publish(title, content, publishedIso, labels) {
  const base = (cfg.labels || []).filter((l) => labels.includes(l));
  const attempts = [labels];
  if (labels.length > 6) attempts.push(labels.slice(0, 6));
  if (JSON.stringify(labels) !== JSON.stringify(base)) attempts.push(base);
  if (base.length) attempts.push([]);
  let lastErr;
  for (let i = 0; i < attempts.length; i++) {
    const body = { title, content };
    if (attempts[i].length) body.labels = attempts[i];
    if (cfg.keep_original_date && publishedIso) body.published = publishedIso;
    try {
      return await bg(`/blogs/${cfg.blogger_blog_id}/posts?isDraft=${cfg.publish_as_draft ? "true" : "false"}`, { method: "POST", body: JSON.stringify(body) });
    } catch (e) {
      lastErr = e;
      if (e.status !== 400 || i === attempts.length - 1) throw e;
      logLine("W", `  400 오류 → 라벨 ${attempts[i + 1].length}개로 줄여 다시 시도합니다`);
    }
  }
  throw lastErr;
}

let running = false, stopFlag = false, busy = false;
function setBusy(b) {
  busy = b;
  ["btnList", "btnLogin", "btnRun", "btnFirstN", "btnAll", "btnNone"].forEach((id) => ($(id).disabled = b || (running && id !== "btnStop")));
  document.querySelectorAll(".tab").forEach((t) => (t.disabled = b || running));
}
$("btnStop").onclick = () => { stopFlag = true; $("btnStop").disabled = true; logLine("W", "현재 글 처리가 끝나면 중지합니다…"); };

// 오래된 글부터 올리기: 글 번호가 있으면 번호순, 아니면 작성일순
function olderFirst(a, b) {
  const na = /^\d+$/.test(a.key), nb = /^\d+$/.test(b.key);
  if (na && nb) return Number(a.key) - Number(b.key);
  return String(a.addDate || "").localeCompare(String(b.addDate || ""));
}

$("btnRun").onclick = async () => {
  if (!cfg[S.idKey] || !posts.length) return alert(`먼저 ${S.blog}의 '글 목록 불러오기'를 해 주세요.`);
  if (!cfg.blogger_blog_id) return alert("먼저 ② 구글 블로거에서 로그인하고 블로그를 선택해 주세요.");
  const targets = checks().filter((c) => c.checked).map((c) => postBySid[sidOfCheck(c)]);
  if (!targets.length) return alert("이전할 글을 선택해 주세요. ('맨 위에서 N개 선택'을 사용할 수 있습니다)");
  if (targets.length > MAX_PER_RUN && !confirm(`한 번에 ${MAX_PER_RUN}개를 넘게 선택하셨습니다 (${targets.length}개).\n\n구글 블로거의 글 작성 제한 때문에 ${MAX_PER_RUN}개 전후에서 '한도 초과' 오류로 멈출 수 있습니다.\n그래도 진행할까요? (취소하고 ${MAX_PER_RUN}개씩 나눠 이전하는 것을 권장합니다)`)) return;
  const redo = targets.filter((p) => isDone(p.sid)).length;
  if (redo && !confirm(`선택한 글 중 ${redo}개는 이미 이전된 글입니다.\n다시 이전하면 블로거에 같은 글이 하나 더 생깁니다. 계속할까요?`)) return;
  if (!confirm(`선택한 글 ${targets.length}개를\n'${cfg.blogger_blog_name}' 블로그로 옮길까요?${cfg.publish_as_draft ? "\n초안으로 저장됩니다." : ""}`)) return;

  targets.sort(olderFirst);
  running = true; stopFlag = false; setBusy(true); $("btnStop").disabled = false;
  let ok = 0, fail = 0; $("bar").style.width = "0%";
  logLine("I", `===== 이전 시작 (${S.label}): ${targets.length}개 =====`);
  try {
    await ensurePy(); await ensureToken();
    for (let i = 0; i < targets.length; i++) {
      if (stopFlag) break;
      const p = targets[i]; const shown = S.titleOf(p); let fatal = false;
      paintRow(p.sid, "진행 중…", "run"); logLine("I", `[${i + 1}/${targets.length}] 이전 중: ${shown}`);
      try {
        const r = await rpc("convert", { cfg: Object.assign({}, cfg), key: p.key, title: S.titleArg(p), addDate: p.addDate || "" });
        const title = r.title || p.title || `(제목 없음 ${p.key})`;
        p.title = title; if (r.date) p.addDate = r.date;
        const tr = rowMap.get(p.sid); if (tr) { tr.children[2].textContent = title; tr.children[1].textContent = p.addDate || "-"; }
        if (cfg.check_images && /<img\b/.test(r.content)) r.content = await fixImages(r.content);
        const m = r.meta;
        logLine("I", `  구조: 소제목 ${m.headings} · 문단 ${m.paragraphs} · 이미지 ${m.images} · 지도 ${m.maps} · 링크 ${m.links} · ${S.tagWord} ${r.tags.length} · FAQ ${m.faq}`);
        const res = await publish(title, r.content, r.published, r.labels);
        state.done[p.sid] = { title, blogger_id: res.id, description: m.description || "", url: res.url, migrated_at: new Date().toISOString() };
        delete state.failed[p.sid]; saveState(); ok++; paintRow(p.sid); logLine("I", `  → 완료: ${res.url}`);
      } catch (e) {
        fail++; logLine("E", `  → 실패: ${e.message}`);
        state.failed[p.sid] = { title: p.title || "", error: String(e.message).slice(0, 300) }; saveState();
        paintRow(p.sid, e.status ? `실패(${e.status})` : "실패", "bad");
        if (e.status === 403 || e.status === 429) fatal = true;
      }
      $("bar").style.width = Math.round(((i + 1) / targets.length) * 100) + "%"; updateSummary();
      if (fatal) { logLine("E", "블로거의 글 작성 제한(한도 초과)에 걸려 작업을 중단합니다. 이미 옮긴 글은 '완료'로 저장되어 있으니, 시간을 충분히 두고 다시 '이전 시작'을 눌러 이어서 진행해 주세요."); break; }
      if (i < targets.length - 1 && !stopFlag) { const end = Date.now() + cfg.delay_between_posts * 1000; while (Date.now() < end && !stopFlag) await sleep(200); }
    }
  } catch (e) { logLine("E", e.message); alert(e.message); }
  running = false; setBusy(false); $("btnStop").disabled = true;
  const head = stopFlag ? "이전이 중지되었습니다." : "이전이 끝났습니다.";
  logLine("I", `===== ${head} 성공 ${ok}개 / 실패 ${fail}개 =====`); updateSummary(); alert(`${head}\n\n성공: ${ok}개\n실패: ${fail}개`);
};

// ---------- 기록 내보내기/가져오기 (현재 선택한 소스 기준) ----------
$("btnExport").onclick = () => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(state, null, 2)], { type: "application/json" }));
  a.download = S.exportName; a.click(); URL.revokeObjectURL(a.href);
};
$("btnImport").onclick = () => $("fileImport").click();
$("fileImport").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  try {
    const d = JSON.parse(await f.text());
    state.done = Object.assign(state.done, d.done || {}); state.failed = Object.assign(state.failed, d.failed || {});
    saveState(); posts.forEach((p) => paintRow(p.sid)); updateSummary(); logLine("I", "이전 기록을 가져왔습니다.");
  } catch { alert(`올바른 이전 기록(${S.exportName}) 파일이 아닙니다.`); }
  e.target.value = "";
};

// ---------- 시작 ----------
{
  let first = "naver";
  try { const q = new URLSearchParams(location.search).get("src"); if (q && SOURCES[q]) first = q; else { const s = store.get("mb_src", "naver"); if (SOURCES[s]) first = s; } } catch {}
  src = first; S = SOURCES[src]; cfg = cfgs[src]; state = states[src];
}
applyTexts();
$("blogId").value = cfg[S.idKey] || "";
$("countN").value = Math.min(MAX_PER_RUN, cfg.batch_count || MAX_PER_RUN);
$("countN").max = MAX_PER_RUN;
if (!FIXED_CID) { setStatus("bloggerStatus", "서비스 설정이 완료되지 않았습니다. 운영자에게 문의해 주세요.", "bad"); $("btnLogin").disabled = true; }
else if (cfg.blogger_blog_id) setStatus("bloggerStatus", `이전에 선택한 블로그: ${cfg.blogger_blog_name} (로그인하면 목록이 갱신됩니다)`, "");
buildOptions();
setOptOpen(store.get("mb_opt_open", true) !== false);
renderTable();
logLine("I", S.startLog);
