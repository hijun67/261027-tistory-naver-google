// 로컬 실행용 서버 (Netlify 없이 내 컴퓨터에서 실행)
//  - public/ 폴더를 그대로 웹으로 제공하고,
//  - /.netlify/functions/tistory , /.netlify/functions/naver 요청을 같은 프록시 코드(netlify/functions)로 처리한다.
//  - 외부 패키지 없이 Node.js 18 이상만 있으면 된다.   실행: npm start   (또는 start-local.bat / start-local.sh)
//
//  옵션(환경변수 또는 인자)
//    PORT=8080            접속 포트 (기본 8080)
//    HOST=127.0.0.1       접속 허용 주소 (기본: 내 컴퓨터에서만. 같은 공유기의 다른 기기에서도 쓰려면 0.0.0.0)
//    --dist               npm run build 로 만든 dist/ 를 제공 (기본은 읽기 쉬운 원본 public/)
//    --open               실행 후 브라우저 자동 열기
//    --ads                화면의 광고 스크립트도 그대로 로드 (기본: 로컬에서는 제거)
"use strict";
const http = require("http");
const fs = require("fs");
const path = require("path");
const { URL } = require("url");
const { spawn } = require("child_process");

const args = new Set(process.argv.slice(2));
const PORT = Number(process.env.PORT) || 8080;
const HOST = process.env.HOST || "127.0.0.1";
const USE_DIST = args.has("--dist");
const OPEN = args.has("--open");
const KEEP_ADS = args.has("--ads");
const ROOT = path.join(__dirname, USE_DIST ? "dist" : "public");

// 로컬에서는 호출 제한을 사실상 해제 (프록시를 require 하기 전에 설정해야 함)
process.env.RATE_LIMIT = process.env.RATE_LIMIT || "1000000";

if (!fs.existsSync(path.join(ROOT, "index.html"))) {
  console.error(`[오류] ${ROOT}/index.html 이 없습니다.` + (USE_DIST ? " 먼저 `npm install && npm run build` 를 실행하세요." : ""));
  process.exit(1);
}

const FUNCTIONS = {
  tistory: require("./netlify/functions/tistory.js").handler,
  naver: require("./netlify/functions/naver.js").handler,
};

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8", ".py": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
  ".xml": "application/xml; charset=utf-8", ".svg": "image/svg+xml", ".webp": "image/webp", ".png": "image/png",
  ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".ico": "image/x-icon", ".bin": "application/octet-stream",
};
const REDIRECTS = { "/privacy": "/privacy.html", "/terms": "/terms.html", "/local": "/local.html" };

function send(res, status, headers, body) {
  res.writeHead(status, Object.assign({ "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" }, headers));
  res.end(body);
}

async function runFunction(name, req, url, res) {
  const headers = Object.assign({}, req.headers, { "x-nf-client-connection-ip": req.socket.remoteAddress || "local" });
  const event = {
    httpMethod: req.method,
    headers,
    queryStringParameters: Object.fromEntries(url.searchParams),
  };
  try {
    const r = await FUNCTIONS[name](event);
    send(res, r.statusCode || 200, r.headers || {}, r.body || "");
  } catch (e) {
    send(res, 500, { "Content-Type": "text/plain; charset=utf-8" }, "function error: " + (e && e.message ? e.message : e));
  }
}

// 로컬에서는 광고 스크립트(외부 광고 서버 호출)를 제거한다.
function stripAds(html) {
  return html
    .replace(/<script[^>]*src=["']https?:\/\/(?:bauval\.org|araplhn\.org)[^>]*>\s*<\/script>/gi, "")
    .replace(/<script>\s*atOptions\s*=[\s\S]*?<\/script>/gi, "");
}

function serveStatic(req, url, res) {
  let p;
  try { p = decodeURIComponent(url.pathname); } catch { return send(res, 400, { "Content-Type": "text/plain; charset=utf-8" }, "bad request"); }
  if (REDIRECTS[p]) p = REDIRECTS[p];
  if (p.endsWith("/")) p += "index.html";
  const file = path.normalize(path.join(ROOT, p));
  if (file !== ROOT && !file.startsWith(ROOT + path.sep)) return send(res, 403, { "Content-Type": "text/plain; charset=utf-8" }, "forbidden");
  fs.stat(file, (err, st) => {
    if (err || !st.isFile()) return send(res, 404, { "Content-Type": "text/plain; charset=utf-8" }, "not found");
    const type = MIME[path.extname(file).toLowerCase()] || "application/octet-stream";
    if (path.basename(file) === "index.html" && !KEEP_ADS) {
      return fs.readFile(file, "utf8", (e, txt) => (e ? send(res, 500, {}, "read error") : send(res, 200, { "Content-Type": type }, stripAds(txt))));
    }
    if (req.method === "HEAD") return send(res, 200, { "Content-Type": type, "Content-Length": st.size }, "");
    res.writeHead(200, { "Content-Type": type, "Content-Length": st.size, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" });
    fs.createReadStream(file).pipe(res);
  });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const m = /^\/\.netlify\/functions\/(tistory|naver)\/?$/.exec(url.pathname);
  if (m) return void runFunction(m[1], req, url, res);
  if (req.method !== "GET" && req.method !== "HEAD") return send(res, 405, { "Content-Type": "text/plain; charset=utf-8" }, "GET only");
  serveStatic(req, url, res);
});

server.on("error", (e) => {
  if (e.code === "EADDRINUSE") console.error(`[오류] ${PORT} 번 포트를 이미 다른 프로그램이 쓰고 있습니다. 다른 번호로 실행하세요.\n  예) Windows: set PORT=8081&& npm start     Mac/Linux: PORT=8081 npm start`);
  else console.error(e);
  process.exit(1);
});

server.listen(PORT, HOST, () => {
  const shown = `http://localhost:${PORT}`;
  console.log("==============================================================");
  console.log(" 블로그 이전 프로그램 (로컬 실행)");
  console.log(`  주소 : ${shown}${USE_DIST ? "   (dist/ 빌드본)" : ""}`);
  console.log(`  안내 : ${shown}/local.html   (사용법 · 상태 점검)`);
  console.log(`  종료 : 이 창에서 Ctrl + C`);
  console.log(" ※ 구글 로그인을 쓰려면 구글 클라우드 > OAuth 클라이언트 >");
  console.log(`    '승인된 JavaScript 원본' 에 ${shown} 을 추가해야 합니다. (README_LOCAL.md 참고)`);
  console.log("==============================================================");
  if (OPEN) {
    const cmd = process.platform === "win32" ? ["cmd", ["/c", "start", "", shown]] : process.platform === "darwin" ? ["open", [shown]] : ["xdg-open", [shown]];
    try { spawn(cmd[0], cmd[1], { stdio: "ignore", detached: true }).on("error", () => {}).unref(); } catch {}
  }
});
