// Pyodide 웹 워커: 파이썬 변환 코어(core_naver.py / core_tistory.py)를 브라우저 안에서 실행한다.
importScripts("https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js");

const SOURCES_OK = ["naver", "tistory"];
let py = null;
let pyLoading = null;
const cores = {};

// @CORE_LOADER_START@ (build.js 가 배포용 코드로 교체합니다. 개발 중에는 core_<소스>.py 를 그대로 읽습니다.)
async function loadCoreSource(src) {
  return await (await fetch("core_" + src + ".py?v=4", { cache: "no-cache" })).text();
}
// @CORE_LOADER_END@

async function initPy() {
  if (py) return;
  if (!pyLoading) {
    pyLoading = (async () => {
      const p = await loadPyodide();
      await p.loadPackage("beautifulsoup4");
      py = p;
    })();
  }
  await pyLoading;
}

async function getCore(src) {
  if (!SOURCES_OK.includes(src)) throw new Error("unknown source " + src);
  if (cores[src]) return cores[src];
  await initPy();
  const code = await loadCoreSource(src);
  py.FS.writeFile("core_" + src + ".py", code);
  cores[src] = py.pyimport("core_" + src);
  return cores[src];
}

function drainLogs(core) {
  const proxy = core.pop_logs();
  const arr = proxy.toJs();
  proxy.destroy();
  return arr;
}

function lastLine(e) {
  const lines = String((e && e.message) || e).trim().split("\n");
  return lines[lines.length - 1];
}

self.onmessage = async (ev) => {
  const { id, type, args } = ev.data;
  const src = args && args.src;
  try {
    const core = await getCore(src);
    let result;
    if (type === "init") {
      result = true;
    } else if (type === "list") {
      result = JSON.parse(core.list_posts(args.id));
    } else if (type === "convert") {
      // 두 코어 모두 convert_post(cfg_json, 글 키, 제목, 작성일) 형태
      result = JSON.parse(core.convert_post(JSON.stringify(args.cfg), args.key, args.title, args.addDate));
    } else {
      throw new Error("unknown type " + type);
    }
    self.postMessage({ id, ok: true, result, logs: drainLogs(core) });
  } catch (e) {
    let logs = [];
    try { if (cores[src]) logs = drainLogs(cores[src]); } catch (_) {}
    self.postMessage({ id, ok: false, error: lastLine(e), logs });
  }
};
