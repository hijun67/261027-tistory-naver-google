// 티스토리 읽기 전용 프록시 (브라우저 CORS 우회용)
// 허용: https://<이름>.tistory.com 의 글 목록/글/RSS/사이트맵 주소만 중계한다.
// 블로그에 사용자 도메인이 연결돼 있으면 티스토리가 보내는 리다이렉트를 따라가되,
// 공개 https/http(기본 포트) 주소만 허용하고 IP·localhost 등 내부 주소는 막는다.
const HOST_RE = /^[a-z0-9][a-z0-9-]*\.tistory\.com$/i;
const PATH_OK = [
  /^\/$/, // 첫 화면 / 목록 (?page=N)
  /^\/rss$/i,
  /^\/sitemap[\w-]*\.xml$/i,
  /^\/(?:m\/)?\d+\/?$/, // 숫자형 글 주소
  /^\/entry\/[^?#]+$/i, // 문자형 글 주소
  /^\/category(?:\/.*)?$/i,
];
const MAX_HOPS = 4;

// ===== 남용 방지 · 크레딧 절약 (naver.js 와 같은 방식) =====
const RATE_LIMIT = Number(process.env.RATE_LIMIT) || 240;            // 같은 IP 가 1분 동안 호출할 수 있는 최대 횟수 (티스토리는 목록 수집 때 호출이 더 많아 넉넉히)
const RATE_WINDOW_MS = 60 * 1000;
const UPSTREAM_TIMEOUT_MS = 8500;  // 전체 대기 제한 (함수 실행 시간 절약)
// 추가로 허용할 사이트 주소(쉼표 구분). Netlify 환경변수 ALLOWED_HOSTS 예) itnes.site,www.itnes.site
const EXTRA_HOSTS = (process.env.ALLOWED_HOSTS || "")
  .split(",")
  .map((x) => x.trim().toLowerCase())
  .filter(Boolean);
const hits = new Map();

function clientIp(h) {
  return h["x-nf-client-connection-ip"] || (h["x-forwarded-for"] || "").split(",")[0].trim() || "unknown";
}
function tooMany(ip) {
  const now = Date.now();
  if (hits.size > 5000) for (const [k, v] of hits) if (now - v.start > RATE_WINDOW_MS) hits.delete(k);
  const rec = hits.get(ip);
  if (!rec || now - rec.start > RATE_WINDOW_MS) { hits.set(ip, { start: now, count: 1 }); return false; }
  rec.count += 1;
  return rec.count > RATE_LIMIT;
}
// 내 사이트 화면(같은 출처)에서 보낸 요청인지 확인
function fromOwnSite(h) {
  if ((h["sec-fetch-site"] || "") === "same-origin") return true;
  const host = (h["host"] || "").toLowerCase();
  const ref = h["referer"] || h["origin"] || "";
  if (!ref) return false;
  try {
    const refHost = new URL(ref).host.toLowerCase();
    return refHost === host || EXTRA_HOSTS.includes(refHost);
  } catch { return false; }
}

const HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  "Accept-Language": "ko-KR,ko;q=0.9",
  Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
};

function reply(status, body, extra = {}) {
  return {
    statusCode: status,
    headers: Object.assign({ "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" }, extra),
    body,
  };
}

function safeRedirect(u) {
  if (u.protocol !== "https:" && u.protocol !== "http:") return false;
  if (u.port && u.port !== "80" && u.port !== "443") return false;
  const h = u.hostname.toLowerCase();
  if (h === "localhost" || h.endsWith(".local") || h.endsWith(".internal") || h.endsWith(".localhost")) return false;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h) || h.includes(":") || h.startsWith("[")) return false; // IP 주소 직접 지정 금지
  return h.includes(".");
}

async function getFollow(startUrl, signal) {
  let cur = startUrl;
  for (let hop = 0; hop <= MAX_HOPS; hop++) {
    const res = await fetch(cur.toString(), { headers: HEADERS, redirect: "manual", signal });
    const loc = res.headers.get("location");
    if (res.status >= 300 && res.status < 400 && loc) {
      let next;
      try {
        next = new URL(loc, cur);
      } catch {
        throw new Error("bad redirect");
      }
      if (!safeRedirect(next)) throw new Error("redirect not allowed");
      cur = next;
      continue;
    }
    return { res, finalUrl: cur.toString() };
  }
  throw new Error("too many redirects");
}

exports.handler = async (event) => {
  if (event.httpMethod !== "GET") return reply(405, "GET only");
  const h = event.headers || {};
  if (!fromOwnSite(h)) return reply(403, "forbidden");
  if (tooMany(clientIp(h))) return reply(429, "too many requests", { "Retry-After": "60" });
  const raw = (event.queryStringParameters || {}).u;
  if (!raw) return reply(400, "missing u");

  let target;
  try {
    target = new URL(raw);
  } catch {
    return reply(400, "bad url");
  }
  if (target.protocol !== "https:" || !HOST_RE.test(target.hostname) || !PATH_OK.some((re) => re.test(target.pathname))) {
    return reply(403, "not allowed");
  }

  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const { res, finalUrl } = await getFollow(target, ctl.signal);
    const buf = Buffer.from(await res.arrayBuffer());
    const ct = res.headers.get("content-type") || "";
    const m = /charset=([\w-]+)/i.exec(ct);
    let text;
    try {
      text = new TextDecoder(m ? m[1] : "utf-8").decode(buf);
    } catch {
      text = buf.toString("utf8");
    }
    return reply(res.status, text, { "X-Final-Url": finalUrl });
  } catch (e) {
    if (e && e.name === "AbortError") return reply(504, "upstream timeout");
    return reply(502, "upstream error: " + (e && e.message ? e.message : e));
  } finally {
    clearTimeout(timer);
  }
};
