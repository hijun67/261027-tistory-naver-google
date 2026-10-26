// 네이버 블로그 읽기 전용 프록시 (브라우저 CORS 우회용)
// 허용된 주소(blog.naver.com 의 글 목록/본문/태그)만 중계한다.
//
// [크레딧 절약 · 남용 방지]
//  1) 내 사이트 화면에서 온 요청만 허용 (다른 사이트/주소창 직접 호출 차단)
//  2) 같은 IP 가 짧은 시간에 너무 많이 호출하면 429 로 차단
//  3) 네이버 응답을 오래 기다리지 않음 (8초) → 함수 실행 시간(=컴퓨트 크레딧) 절약
//
// ※ 헤더 검사는 일반 브라우저/봇을 걸러 내는 용도입니다. curl 등으로 헤더를
//   위조하는 호출까지 완전히 막지는 못하므로, 호출 제한(2)과 함께 사용합니다.
const ALLOWED_HOST = "blog.naver.com";
const ALLOWED_PATHS = new Set([
  "/PostTitleListAsync.naver",
  "/PostView.naver",
  "/BlogTagListInfo.naver",
]);

// ===== 설정 (필요하면 여기만 바꾸세요) =====
const RATE_LIMIT = Number(process.env.RATE_LIMIT) || 120;            // 같은 IP 가 1분 동안 호출할 수 있는 최대 횟수
const RATE_WINDOW_MS = 60 * 1000;  // 호출 횟수를 세는 시간 창
const UPSTREAM_TIMEOUT_MS = 8000;  // 네이버 응답 대기 제한
// 추가로 허용할 사이트 주소(쉼표 구분). Netlify > Environment variables 에 ALLOWED_HOSTS 로 넣을 수 있음
// 예) itnes.site,www.itnes.site
const EXTRA_HOSTS = (process.env.ALLOWED_HOSTS || "")
  .split(",")
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean);
// ==========================================

const HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  Referer: "https://blog.naver.com/",
  "Accept-Language": "ko-KR,ko;q=0.9",
};

// 같은 함수 인스턴스 안에서만 유지되는 간단한 호출 기록 (완벽하지 않지만 폭주를 줄여 줌)
const hits = new Map();

function reply(status, body, extra = {}) {
  return {
    statusCode: status,
    headers: Object.assign(
      { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
      extra
    ),
    body,
  };
}

function clientIp(h) {
  return (
    h["x-nf-client-connection-ip"] ||
    (h["x-forwarded-for"] || "").split(",")[0].trim() ||
    "unknown"
  );
}

function tooMany(ip) {
  const now = Date.now();
  if (hits.size > 5000) {
    for (const [k, v] of hits) if (now - v.start > RATE_WINDOW_MS) hits.delete(k);
  }
  const rec = hits.get(ip);
  if (!rec || now - rec.start > RATE_WINDOW_MS) {
    hits.set(ip, { start: now, count: 1 });
    return false;
  }
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
  } catch {
    return false;
  }
}

exports.handler = async (event) => {
  if (event.httpMethod !== "GET") return reply(405, "GET only");

  const h = event.headers || {};
  if (!fromOwnSite(h)) return reply(403, "forbidden");
  if (tooMany(clientIp(h))) {
    return reply(429, "too many requests", { "Retry-After": "60" });
  }

  const raw = (event.queryStringParameters || {}).u;
  if (!raw) return reply(400, "missing u");

  let target;
  try {
    target = new URL(raw);
  } catch {
    return reply(400, "bad url");
  }
  if (target.protocol !== "https:" || target.hostname !== ALLOWED_HOST || !ALLOWED_PATHS.has(target.pathname)) {
    return reply(403, "not allowed");
  }

  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const res = await fetch(target.toString(), { headers: HEADERS, redirect: "follow", signal: ctl.signal });
    const buf = Buffer.from(await res.arrayBuffer());
    const ct = res.headers.get("content-type") || "";
    const m = /charset=([\w-]+)/i.exec(ct);
    let text;
    try {
      text = new TextDecoder(m ? m[1] : "utf-8").decode(buf);
    } catch {
      text = buf.toString("utf8");
    }
    return reply(res.status, text);
  } catch (e) {
    if (e && e.name === "AbortError") return reply(504, "upstream timeout");
    return reply(502, "upstream error: " + (e && e.message ? e.message : e));
  } finally {
    clearTimeout(timer);
  }
};
