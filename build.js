// 배포용 빌드: public/ (읽기 쉬운 원본) -> dist/ (난독화·압축본)
//  - app.js / worker.js / config.js : terser 압축 + javascript-obfuscator 난독화
//  - core_naver.py / core_tistory.py : zlib 압축 + 랜덤 키 XOR -> core_naver.bin / core_tistory.bin
//  - index.html : 주석 제거 (맨 위 build 표식 주석은 유지)
//  - 그 밖의 파일(이미지, sw.js, privacy/terms, sitemap, robots 등)은 그대로 복사
// 사용: npm install && npm run build   (난독화 없이 확인: npm run build:debug)
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const crypto = require("crypto");

const SRC = path.join(__dirname, "public");
const OUT = path.join(__dirname, "dist");
const OBF = process.env.OBF !== "0";
const DOMAIN_LOCK = (process.env.DOMAIN_LOCK || "").split(",").map((s) => s.trim()).filter(Boolean); // 예: DOMAIN_LOCK=itnes.site,www.itnes.site
const CORES = ["naver", "tistory"];
const JS_FILES = ["app.js", "config.js"]; // worker.js 는 따로 처리

let terser = null, JSO = null;
if (OBF) {
  try { terser = require("terser"); JSO = require("javascript-obfuscator"); }
  catch (e) { console.error("terser / javascript-obfuscator 가 없습니다. 먼저 `npm install` 을 실행하세요."); process.exit(1); }
}

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

async function protectJs(code, { worker = false } = {}) {
  if (!OBF) return code;
  const min = await terser.minify(code, { compress: { passes: 2 }, mangle: { toplevel: false }, format: { comments: false } });
  if (min.error) throw min.error;
  const opts = {
    compact: true,
    target: worker ? "browser-no-eval" : "browser",
    renameGlobals: false,
    identifierNamesGenerator: "hexadecimal",
    stringArray: true,
    stringArrayEncoding: ["base64"],
    stringArrayThreshold: 0.8,
    stringArrayWrappersCount: 2,
    splitStrings: true,
    splitStringsChunkLength: 8,
    controlFlowFlattening: true,
    controlFlowFlatteningThreshold: 0.4,
    deadCodeInjection: true,
    deadCodeInjectionThreshold: 0.15,
    numbersToExpressions: true,
    simplify: true,
    unicodeEscapeSequence: false,
    sourceMap: false,
  };
  if (DOMAIN_LOCK.length) { opts.domainLock = DOMAIN_LOCK; opts.domainLockRedirectUrl = "about:blank"; }
  return JSO.obfuscate(min.code, opts).getObfuscatedCode();
}

// 모든 코어를 같은 키로 압축·XOR (키는 빌드마다 새로 생성)
function packCores() {
  const key = crypto.randomBytes(16);
  for (const c of CORES) {
    const raw = fs.readFileSync(path.join(SRC, `core_${c}.py`));
    const buf = Buffer.from(zlib.deflateSync(raw, { level: 9 }));
    for (let i = 0; i < buf.length; i++) buf[i] ^= key[i % key.length];
    fs.writeFileSync(path.join(OUT, `core_${c}.bin`), buf);
  }
  return [...key];
}

const LOADER_RE = /\/\/ @CORE_LOADER_START@[\s\S]*?\/\/ @CORE_LOADER_END@/;
function packedLoader(key) {
  return `async function loadCoreSource(src) {
  const buf = new Uint8Array(await (await fetch("core_" + src + ".bin?v=4", { cache: "no-cache" })).arrayBuffer());
  const k = ${JSON.stringify(key)};
  for (let i = 0; i < buf.length; i++) buf[i] ^= k[i % k.length];
  const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("deflate"));
  return await new Response(stream).text();
}`;
}

(async () => {
  const key = packCores();

  let worker = fs.readFileSync(path.join(SRC, "worker.js"), "utf8");
  if (!LOADER_RE.test(worker)) throw new Error("worker.js 에서 CORE_LOADER 표식을 찾지 못했습니다.");
  worker = worker.replace(LOADER_RE, packedLoader(key));
  fs.writeFileSync(path.join(OUT, "worker.js"), await protectJs(worker, { worker: true }));

  for (const f of JS_FILES) fs.writeFileSync(path.join(OUT, f), await protectJs(fs.readFileSync(path.join(SRC, f), "utf8")));

  let html = fs.readFileSync(path.join(SRC, "index.html"), "utf8");
  if (OBF) html = html.replace(/<!--[\s\S]*?-->/g, (m) => (m.startsWith("<!-- build:") ? m : ""));
  fs.writeFileSync(path.join(OUT, "index.html"), html);

  // 나머지 정적 파일 복사
  const handled = new Set(["index.html", "app.js", "config.js", "worker.js", ...CORES.map((c) => `core_${c}.py`)]);
  for (const f of fs.readdirSync(SRC)) {
    if (handled.has(f) || f.endsWith(".py") || f === "local.html") continue; // local.html 은 로컬 실행 전용 안내문 // 파이썬 원본은 core_*.bin 으로만 배포
    const full = path.join(SRC, f);
    if (!fs.statSync(full).isFile()) continue;
    fs.copyFileSync(full, path.join(OUT, f));
  }
  console.log(`빌드 완료 -> dist/  (난독화 ${OBF ? "ON" : "OFF"}${DOMAIN_LOCK.length ? ", 도메인 잠금 " + DOMAIN_LOCK.join(",") : ""})`);
})().catch((e) => { console.error(e); process.exit(1); });
