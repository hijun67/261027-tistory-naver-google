# -*- coding: utf-8 -*-
"""
네이버 블로그 → 블로거 변환 코어 (웹 버전, Pyodide 에서 실행)
원본 naver_to_blogger_gui 의 변환 로직을 그대로 사용하고, 네트워크/GUI/구글 인증 부분만 웹용으로 교체했습니다.
"""
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote_plus

from bs4 import BeautifulSoup, NavigableString, Tag

KST = timezone(timedelta(hours=9))

DEFAULT_CONFIG = {
    "naver_blog_id": "",
    "blogger_blog_id": "",
    "blogger_blog_name": "",
    "keep_original_date": True,
    "publish_as_draft": False,
    "labels": ["네이버블로그"],
    "delay_between_posts": 5,
    "batch_count": 10,
    "para_max_chars": 100,
    "para_max_sentences": 3,
    "pc_line_chars": 36,
    "wrap_chars": 28,
    "auto_heading": True,
    "add_toc": True,
    "add_json_ld": True,
    "import_maps": True,
    "hashtags_in_body": True,
    "hashtags_as_labels": True,
    "keep_original_paragraphs": False,
    "image_width": 966,
    "import_cover": True,
    "center_all": True,
}

# ---- 로그: 파일 대신 메모리에 모아서 화면에 보여준다 ----
LOG_LINES = []


class _ListHandler(logging.Handler):
    def emit(self, record):
        LOG_LINES.append(record.levelname[0] + "|" + self.format(record))


log = logging.getLogger("n2b")
log.setLevel(logging.INFO)
log.handlers.clear()
_h = _ListHandler()
_h.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_h)


def pop_logs():
    out = list(LOG_LINES)
    LOG_LINES.clear()
    return out


# ---- 네트워크: 브라우저 동기 XHR → Netlify 프록시 함수 ----
class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


class _Session:
    def get(self, url, params=None, timeout=None):
        from js import XMLHttpRequest  # Pyodide(웹 워커) 전용
        qs = "&".join("%s=%s" % (quote(str(k)), quote(str(v))) for k, v in (params or {}).items())
        full = url + ("?" + qs if qs else "")
        x = XMLHttpRequest.new()
        x.open("GET", "/.netlify/functions/naver?u=" + quote(full, safe=""), False)
        x.send()
        return _Resp(int(x.status), str(x.responseText))


SESSION = _Session()

# ============================================================
# 네이버 블로그 수집
# ============================================================
def extract_naver_id(text):
    """아이디 또는 블로그 주소에서 네이버 블로그 ID 추출"""
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"blogId=([A-Za-z0-9_\-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"naver\.com/([A-Za-z0-9_\-]+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-]+", text):
        return text
    return None


def _parse_list_response(text):
    """PostTitleListAsync 응답에서 (logNo, title, addDate) 목록 추출"""
    items = []
    cleaned = text.replace("\\'", "'")
    try:
        data = json.loads(cleaned, strict=False)
        for p in data.get("postList", []):
            items.append(
                {
                    "logNo": str(p.get("logNo")),
                    "title": html.unescape(unquote_plus(p.get("title", ""))),
                    "addDate": p.get("addDate", ""),
                }
            )
        return items
    except Exception:
        pass

    # JSON 파싱 실패 시 정규식으로 대체
    pattern = re.compile(
        r'"logNo"\s*:\s*"?(\d+)"?.*?"title"\s*:\s*"(.*?)".*?"addDate"\s*:\s*"(.*?)"',
        re.S,
    )
    for log_no, title, add_date in pattern.findall(cleaned):
        items.append(
            {
                "logNo": log_no,
                "title": html.unescape(unquote_plus(title)),
                "addDate": add_date,
            }
        )
    return items


def fetch_post_list(blog_id, on_page=None):
    """네이버 블로그 전체 글 목록 (페이지 순회)"""
    url = "https://blog.naver.com/PostTitleListAsync.naver"
    posts, seen = [], set()
    page = 1
    while True:
        params = {
            "blogId": blog_id,
            "viewdate": "",
            "currentPage": page,
            "categoryNo": "",
            "parentCategoryNo": "",
            "countPerPage": 30,
        }
        resp = SESSION.get(url, params=params, timeout=20)
        resp.raise_for_status()
        items = _parse_list_response(resp.text)
        new_items = [i for i in items if i["logNo"] not in seen]
        if not new_items:
            break
        for i in new_items:
            seen.add(i["logNo"])
            posts.append(i)
        if on_page:
            on_page(page, len(posts))
        page += 1
        time.sleep(0.5)
    return posts


# ============================================================
# 본문 변환 : 모바일 가독성 + SEO / GEO 구조화
# ============================================================
# 변환 원칙
#  1) 네이버 편집기 구조(스마트에디터 ONE / 구 에디터)를 '줄 → 문단 → 블록'으로 다시 짠다.
#  2) 문단  : 문장 단위로 나눠 '한 문단 N자 · M문장 이내'로 묶는다 (모바일 3~5줄).
#            줄바꿈은 브라우저에 맡기고, 한글 어절 단위(keep-all)로 끊기게 한다.
#  3) 소제목 : 굵은 한 줄 / 큰 글씨 / 소제목 컴포넌트 → h2·h3 (글 제목 h1 은 블로거 테마가 담당).
#  4) 구조화 : 목록 → ul/ol, 표 → 가로 스크롤 표(첫 행 th), Q./A. → h3 + 답변 (+ FAQPage JSON-LD).
#  5) 이미지 : alt / width / height / lazy-loading, figure + figcaption.
#  6) GEO   : 서두 문단 보존, 목차(nav), 문단별 자기완결적 길이.
#  7) 블로거의 '줄바꿈 변환' 설정이 켜져 있어도 깨지지 않도록 결과 HTML 에는 개행 문자를 넣지 않는다.

_TAG_RE = re.compile(r"<[^>]+>")
_TAG_PARSE_RE = re.compile(r"<(/?)([a-zA-Z0-9]+)([^>]*)>")
_VOID_TAGS = {"br", "img", "hr"}

# ---- 인라인 스타일 (블로거 테마와 무관하게 동일하게 보이도록 inline 으로 지정) ----
_WRAP = (
    "max-width:{em}em;margin:0;padding:0;box-sizing:border-box;text-align:left;"
    "color:#222;font-size:16px;line-height:1.85;"
    "word-break:keep-all;overflow-wrap:break-word;"
)
ST_P = (
    "margin:0 0 1.15em;font-size:16px;line-height:1.85;text-align:left;"
    "word-break:keep-all;overflow-wrap:break-word;"
)
ST_H2 = (
    "margin:2.2em 0 .8em;padding:0 0 .35em;font-size:1.35em;font-weight:700;"
    "line-height:1.45;text-align:left;border-bottom:2px solid #eee;"
    "word-break:keep-all;overflow-wrap:break-word;"
)
ST_H3 = (
    "margin:1.8em 0 .6em;font-size:1.15em;font-weight:700;line-height:1.5;"
    "text-align:left;word-break:keep-all;overflow-wrap:break-word;"
)
ST_LIST = "margin:0 0 1.2em;padding-left:1.4em;text-align:left;"
ST_LI = (
    "margin:0 0 .45em;line-height:1.8;text-align:left;"
    "word-break:keep-all;overflow-wrap:break-word;"
)
ST_FIGURE = "margin:1.5em 0;padding:0;text-align:center;"
ST_IMG = "display:block;max-width:100%;height:auto;margin:0 auto;"
ST_CAP = "margin:.5em 0 0;font-size:.85em;line-height:1.5;color:#777;text-align:center;"
ST_QUOTE = (
    "margin:1.4em 0;padding:.7em 1em;border-left:3px solid #ccc;"
    "background:#fafafa;color:#555;text-align:left;"
)
ST_QUOTE_P = "margin:0 0 .5em;line-height:1.8;word-break:keep-all;overflow-wrap:break-word;"
ST_HR = "margin:2em 0;border:0;border-top:1px solid #ddd;"
ST_TABLE_WRAP = "margin:1.3em 0;overflow-x:auto;-webkit-overflow-scrolling:touch;"
ST_TABLE = "border-collapse:collapse;width:100%;min-width:320px;font-size:.92em;line-height:1.6;"
ST_CELL = (
    "border:1px solid #ddd;padding:.5em .6em;text-align:left;vertical-align:top;"
    "word-break:keep-all;overflow-wrap:break-word;"
)
ST_TH = ST_CELL + "background:#f6f6f6;font-weight:700;"
ST_PRE = (
    "margin:1.3em 0;padding:.8em 1em;overflow-x:auto;background:#f6f6f6;"
    "border-radius:4px;font-size:.85em;line-height:1.55;white-space:pre;text-align:start;"
)
ST_TOC = (
    "margin:0 0 1.8em;padding:.9em 1.1em;border:1px solid #e5e5e5;"
    "border-radius:8px;background:#fafafa;text-align:left;"
)
ST_FOOT = (
    "margin:0;font-size:13px;line-height:1.7;color:#888;text-align:left;"
    "word-break:break-all;"
)

ST_TAGS = (
    "margin:1.6em 0 0;font-size:.92em;line-height:2;color:#555;text-align:left;"
    "word-break:keep-all;overflow-wrap:break-word;"
)
ST_TAG = "display:inline-block;margin:0 .6em 0 0;color:#2a6df4;"
ST_MAP_FIG = "margin:1.5em 0;padding:0;text-align:left;"
ST_MAP_BOX = (
    "position:relative;width:100%;height:0;padding-bottom:56%;overflow:hidden;"
    "border-radius:6px;background:#eee;"
)
ST_MAP_FRAME = "position:absolute;top:0;left:0;width:100%;height:100%;border:0;"
ST_ROW = "display:flex;gap:4px;margin:1.5em 0;align-items:flex-start;"
ST_ROW_CELL = "flex:1 1 0;min-width:0;margin:0;padding:0;text-align:center;"
ST_IMG_ROW = "display:block;width:100%;height:auto;margin:0;"
ST_LINKCARD = (
    "display:flex;gap:0;margin:1.2em 0;border:1px solid #e5e5e5;border-radius:8px;"
    "overflow:hidden;text-align:left;"
)
ST_LINKCARD_TXT = (
    "flex:1 1 auto;min-width:0;padding:.7em .9em;line-height:1.6;"
    "word-break:keep-all;overflow-wrap:anywhere;"
)
ST_MAP_CAP = (
    "margin:.6em 0 0;font-size:.9em;line-height:1.6;color:#555;text-align:left;"
    "word-break:keep-all;overflow-wrap:break-word;"
)


# ---------------------------------------------------------------
# 작은 도구들
# ---------------------------------------------------------------
def plain_text(markup):
    """HTML 조각 → 태그 없는 문자열"""
    return html.unescape(_TAG_RE.sub("", markup or "")).strip()


def _to_int(value):
    try:
        return int(float(str(value).strip()))
    except Exception:
        return None


def _strip_strong(markup):
    return re.sub(r"</?strong[^>]*>", "", markup)


def _clean_line(markup):
    """빈 태그·중복 공백 정리"""
    prev = None
    while prev != markup:
        prev = markup
        markup = re.sub(
            r"<(strong|em|u|s|a|sup|sub|mark|code|small|span)(?:\s[^>]*)?></\1>", "", markup
        )
    return re.sub(r"\s{2,}", " ", markup).strip()


def balance(fragment, carry=None):
    """
    태그가 중간에서 잘린 HTML 조각을 짝이 맞게 보정한다.
    carry: 앞 조각에서 열려 있던 [(태그명, 여는태그)] → 이 조각 앞에 다시 열어 준다.
    반환: (보정된 HTML, 다음 조각으로 넘길 carry)
    """
    stack = list(carry or [])
    parts = ["".join(opener for _, opener in stack)]
    pos = 0
    for m in _TAG_PARSE_RE.finditer(fragment):
        parts.append(fragment[pos:m.start()])
        pos = m.end()
        closing, name, rest = m.group(1), m.group(2).lower(), m.group(3)
        if name in _VOID_TAGS or rest.rstrip().endswith("/"):
            parts.append(m.group(0))
            continue
        if closing:
            if any(n == name for n, _ in stack):
                while stack:
                    n, _ = stack.pop()
                    parts.append(f"</{n}>")
                    if n == name:
                        break
            # 짝 없는 닫는 태그는 버린다
        else:
            stack.append((name, m.group(0)))
            parts.append(m.group(0))
    parts.append(fragment[pos:])
    carry_out = list(stack)
    parts.extend(f"</{n}>" for n, _ in reversed(stack))
    return "".join(parts), carry_out


# ---------------------------------------------------------------
# 문장 분리 (태그 보존)
# ---------------------------------------------------------------
_END_PUNCT = r"[.!?。！？…]+[\"'”’)\]」』]*"
_END_INSIDE = re.compile(_END_PUNCT + r"(?=\s)")
_END_AT_TAIL = re.compile(_END_PUNCT + r"$")
_ABBR_RE = re.compile(r"\b(?:e\.g|i\.e|vs|Dr|Mr|Mrs|Ms|St|No)\.$")


def _is_false_boundary(prefix_markup):
    """'1.' 'A.' 'e.g.' 같은 곳에서는 문장을 끊지 않는다."""
    p = plain_text(prefix_markup)
    return bool(re.fullmatch(r"(?:\d{1,3}|[A-Za-z])[.)]", p) or _ABBR_RE.search(p))


def split_sentences(line_markup):
    """한 줄(HTML)을 문장 단위 HTML 조각 목록으로 나눈다. (태그 균형은 호출 측에서 보정)"""
    tokens = [t for t in re.split(r"(<[^>]+>)", line_markup) if t]
    sentences, cur, pending = [], [], False

    for tok in tokens:
        if tok.startswith("<") and tok.endswith(">"):
            cur.append(tok)
            continue

        if pending:
            pending = False
            if tok[:1].isspace() and not _is_false_boundary("".join(cur)):
                sentences.append("".join(cur).strip())
                cur = []
                tok = tok.lstrip()
                if not tok:
                    continue

        pos = 0
        for m in _END_INSIDE.finditer(tok):
            end = m.end()
            if _is_false_boundary("".join(cur) + tok[pos:end]):
                continue
            cur.append(tok[pos:end])
            sentences.append("".join(cur).strip())
            cur = []
            pos = end + (len(tok[end:]) - len(tok[end:].lstrip()))

        rest = tok[pos:]
        if rest:
            cur.append(rest)
            if _END_AT_TAIL.search(rest):
                pending = True

    if cur:
        sentences.append("".join(cur).strip())
    return [s for s in sentences if plain_text(s)]


def pack_prose(lines, max_chars, max_sentences):
    """
    줄 목록 → 모바일용 문단 목록.
    각 줄을 문장으로 쪼갠 뒤 (max_chars 글자, max_sentences 문장) 이내로 묶는다.
    """
    units = []
    for line in lines:
        carry = []
        for s in split_sentences(line):
            fixed, carry = balance(s, carry)
            if plain_text(fixed):
                units.append(fixed)

    paras, cur, cur_len = [], [], 0
    for u in units:
        n = len(plain_text(u))
        if cur and (cur_len + n > max_chars or len(cur) >= max_sentences):
            paras.append(cur)
            cur, cur_len = [], 0
        cur.append(u)
        cur_len += n + 1
    if cur:
        paras.append(cur)

    # 마지막 문단이 너무 짧은 한 문장이면 앞 문단에 붙인다 (고아 줄 방지)
    if len(paras) >= 2 and len(paras[-1]) == 1:
        tail_len = len(plain_text(paras[-1][0]))
        prev_len = sum(len(plain_text(x)) + 1 for x in paras[-2])
        if (tail_len < 25 and len(paras[-2]) < max_sentences
                and prev_len + tail_len <= max_chars * 1.3):
            paras[-2].extend(paras.pop())

    return [" ".join(p) for p in paras]


# ---------------------------------------------------------------
# '*항목' 분리 / 의미 단위 줄바꿈 / '#취소#확인' 찌꺼기 제거
# ---------------------------------------------------------------
_UI_JUNK_TAGS = {"취소", "확인"}          # 네이버 화면 버튼 글자가 해시태그로 딸려오는 경우
_JUNK_TAG_RE = re.compile(
    r"(?:(?<=\s)|(?<=>)|^)(?:#\s*(?:취소|확인))+(?=\s|<|$)"
)


def _strip_junk_tags(markup):
    """줄 안의 '#취소' '#확인' 제거 (진짜 해시태그 '#확인사항' 등은 그대로 둔다)"""
    out = _JUNK_TAG_RE.sub("", markup)
    if out == markup:
        return markup
    return re.sub(r"\s{2,}", " ", out).strip()


_STAR_HEAD_RE = re.compile(r"^[\x00\s]*\*(?!\*)")


def _split_star_items(line):
    """
    '*출발지:A *도착지:B *소요 시간:C' 처럼 한 줄에 이어 쓴 '*항목'을 항목별 줄로 나눈다.
    나뉜 항목은 '* 항목' 형태로 맞춰 목록(불릿)으로 인식되게 한다. (태그 균형도 보정)
    줄 중간에 '*' 가 없거나 'A * B' 처럼 공백으로 둘러싸인 곱셈 표시는 그대로 둔다.
    """
    if "*" not in line:
        return [line]
    masked = _TAG_RE.sub(lambda m: "\x00" * len(m.group(0)), line)
    head = _STAR_HEAD_RE.match(masked)
    if head:                                    # 줄이 '*' 로 시작하면 ' * ' 모양도 구분자로 본다
        pat = re.compile(r"(?<![^\s\x00])\*(?!\*)")
    else:
        pat = re.compile(r"(?<![^\s\x00])\*(?![\s*]|$)")
    head_pos = head.end() - 1 if head else -1
    cuts = [m.start() for m in pat.finditer(masked)]
    inline = [p for p in cuts if p > head_pos]
    if not inline:
        return [line]

    bounds = sorted(set([0] + cuts)) + [len(line)]
    out, carry = [], []
    for a, b in zip(bounds, bounds[1:]):
        piece = line[a:b].strip()
        if not piece:
            continue
        fixed, carry = balance(piece, carry)
        if not plain_text(fixed):
            continue
        fixed = re.sub(r"^((?:<[^>]+>)*)\*\s*", r"\1* ", fixed)
        out.append(fixed)
    return out or [line]


# --- 의미 단위(구) 줄바꿈 -------------------------------------------------
_PHRASE_END_RE = re.compile(
    r"(?:에서는|에서도|에서의|에서|에게|에는|에도|으로|로서|로써|까지|부터|처럼|보다|"
    r"이며|하며|하고|해서|하여|이고|이나|라도|인데|는데|지만|으며|면서|거나|니까|"
    r"입니다|합니다|습니다|해요|에요|예요|세요|드립니다)$"
)
_ONE_PARTICLE_RE = re.compile(r"[은는이가을를의에도만로와과]$")
_PUNCT_END_RE = re.compile(r"[,，、;；:：)\]」』”’\"'>…!?.。！？\-]$")
_OPEN_START_RE = re.compile(r"^[\-(\[「『“‘\"*#]")
_NO_BREAK_BEFORE = {"수", "것", "등", "때", "중", "후", "전", "및", "분", "명", "개", "원",
                    "번", "회", "건", "km", "m", "kg", "%", "시간", "일", "월", "년"}
_DANGLING_END = {"약", "총", "및", "또는", "그리고", "~", "-", "/", "·", "+", "&", "*", "#"}


def _vis_len(text):
    """눈에 보이는 폭 (한글 1, 영문·숫자·기호·공백 약 0.55)"""
    return sum(0.55 if ord(c) < 128 else 1.0 for c in text)


def _split_words(markup):
    """HTML 한 줄 → 어절 목록 (태그는 붙어 있는 어절에 딸려 간다)"""
    words, cur = [], []
    for tok in re.split(r"(<[^>]+>)", markup):
        if not tok:
            continue
        if tok.startswith("<") and tok.endswith(">"):
            cur.append(tok)
            continue
        for part in re.split(r"(\s+)", tok):
            if not part:
                continue
            if part.isspace():
                if cur:
                    words.append("".join(cur))
                    cur = []
            else:
                cur.append(part)
    if cur:
        words.append("".join(cur))
    merged, pend = [], ""
    for w in words:                       # 글자 없이 태그만 있는 어절은 이웃에 붙인다
        if not plain_text(w):
            if merged:
                merged[-1] += w
            else:
                pend += w
            continue
        merged.append(pend + w)
        pend = ""
    if pend and merged:
        merged[-1] += pend
    return merged


def _break_penalty(prev, nxt):
    """prev 어절 뒤에서 줄을 끊을 때의 어색함 (작을수록 좋은 끊는 자리)"""
    p, n = plain_text(prev), plain_text(nxt)
    pen = 5
    if _PUNCT_END_RE.search(p) or _OPEN_START_RE.search(n):
        pen = 0
    elif _PHRASE_END_RE.search(p):
        pen = 1
    elif _ONE_PARTICLE_RE.search(p):
        pen = 3
    if p in _DANGLING_END or re.search(r"[(\[「『“‘]$", p):
        pen += 10
    if n.strip(".,)") in _NO_BREAK_BEFORE:
        pen += 8
    if len(p) <= 1:
        pen += 4
    return pen


def soft_wrap(markup, max_w):
    """
    긴 문장을 의미 단위(구)로 묶어 <span style="display:inline-block">…</span> 로 감싼다.
    구 하나는 중간에서 끊기지 않으므로, 줄이 넘칠 때 구와 구 사이에서만 줄바꿈된다.
    (<br> 로 강제 개행하지 않아서 PC·모바일 어느 폭에서도 어색하게 한두 글자가 남지 않는다)
      max_w : 구 하나의 최대 폭(한글 기준 글자수). 0 이하면 끔.
    """
    if not max_w or max_w <= 0 or "<br" in markup:
        return markup
    words = _split_words(markup)
    n = len(words)
    if n < 2:
        return markup
    lens = [_vis_len(plain_text(w)) for w in words]
    if sum(lens) + 0.55 * (n - 1) <= max_w:
        return markup                       # 한 줄에 들어오는 짧은 글은 그대로

    INF = float("inf")
    best = [INF] * (n + 1)
    back = [0] * (n + 1)
    best[0] = 0.0
    for j in range(1, n + 1):
        width = 0.0
        for i in range(j - 1, -1, -1):
            width += lens[i] + (0.55 if i < j - 1 else 0)
            if width > max_w and i < j - 1:
                break
            cost = 100.0
            if width > max_w:
                cost += 50                  # 어절 하나가 기준보다 긴 경우
            elif j < n:
                cost += ((max_w - width) / max_w) ** 2 * 12
            if j < n:
                cost += _break_penalty(words[j - 1], words[j])
            if width < 6:
                cost += 6
            if best[i] + cost < best[j]:
                best[j] = best[i] + cost
                back[j] = i
    chunks, j = [], n
    while j > 0:
        chunks.append((back[j], j))
        j = back[j]
    chunks.reverse()
    if len(chunks) < 2:
        return markup

    spans, carry = [], []
    for a, b in chunks:
        fixed, carry = balance(" ".join(words[a:b]), carry)
        spans.append(f'<span style="display:inline-block;">{fixed}</span>')
    return " ".join(spans)


# ---------------------------------------------------------------
# 광고 판별 / 지도 정보 추출 / 지도 HTML
# ---------------------------------------------------------------
# ---------------------------------------------------------------
# 줄 이어붙이기 / 정렬 / 색상 도우미
# ---------------------------------------------------------------
_SENT_END_RE = re.compile(
    r"[.!?。！？…~)\]」』”’\"'>:：;]$|[ㅎㅋㅠㅜ]+$|[\U0001F000-\U0001FAFF\u2600-\u27BF]\ufe0f?$"
)


_LABEL_START_RE = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50※★☆]")
_LABEL_END_RE = re.compile(r"(?:tip|팁)$", re.I)


def _is_label_line(markup):
    """'💡 꿀 Tip' 처럼 뒤 내용을 소개하는 짧은 라벨 줄인가"""
    p = plain_text(markup)
    return bool(p and len(p) <= 28 and not _RE_BULLET.match(markup)
                and (_LABEL_START_RE.match(p) or _LABEL_END_RE.search(p)))


def _prejoin(lines, hk_all, auto_heading, faq=False, max_line=32):
    """
    좁은 폭에 맞춰 한 문장을 여러 줄로 끊어 쓴 글을 의미 단위로 다시 이어 붙인다. (분류 전에 실행)
      - 짧고(max_line 자 이하) 마침표 등으로 끝나지 않은 줄은 다음 줄과 합친다.
      - 단, 다음 줄이 '새 항목'(번호·불릿·'항목: 값'·Q/A) 또는 소제목이면 합치지 않는다.
      - '3) SUV 급 차량' 같은 짧은 번호 제목, '💡 꿀 Tip' 같은 라벨 줄은 뒤 줄을 삼키지 않는다.
    """
    n = len(lines)
    if n < 2:
        return list(lines)
    plains = [plain_text(l) for l in lines]

    def is_heading(i):
        if not (auto_heading and hk_all[i]):
            return False
        if hk_all[i] == "bold" and ((i > 0 and hk_all[i - 1]) or (i + 1 < n and hk_all[i + 1])):
            return False
        return True

    def starts_item(i):
        l, p = lines[i], plains[i]
        return bool(
            _RE_Q.match(l) or _RE_NUMBER.match(l) or _RE_BULLET.match(l)
            or (faq and _RE_A.match(l))
            or (_RE_KV.match(p) and not re.search(r"[.!?。]$", p))
        )

    def closed(i):
        """i 번째 줄이 여기서 끝나는 줄(뒤 줄을 붙이지 않음)인가"""
        p, l = plains[i], lines[i]
        if not p or len(p) > max_line or _SENT_END_RE.search(p):
            return True
        if re.match(r"^(?:<[^>]+>)*\s*\*(?!\*)", l):         # '*출발 시간 :…' 별표 항목은 한 줄로 끝
            return True
        if is_heading(i):
            return True
        if _RE_NUMBER.match(l) and len(p) <= 26:          # '3) SUV 급 차량' 같은 번호 제목
            return True
        if (not _RE_BULLET.match(l) and len(p) <= 28
                and (_LABEL_START_RE.match(p) or _LABEL_END_RE.search(p))):   # '💡 꿀 Tip'
            return True
        return False

    out = [lines[0]]
    for i in range(1, n):
        if (plains[i] and not closed(i - 1)
                and not starts_item(i) and not is_heading(i)):
            out[-1] += " " + lines[i]
        else:
            out.append(lines[i])
    return out


def _align_of(classes, node):
    """문단 정렬: 'center' / 'right' / '' (왼쪽·양쪽) / None (지정 없음)"""
    for c in classes:
        m = re.fullmatch(r"se-text-paragraph-align-(\w+)", c)
        if m:
            return m.group(1) if m.group(1) in ("center", "right") else ""
    style = (node.get("style") or "").lower()
    m = re.search(r"text-align\s*:\s*(center|right|left|justify)", style)
    if not m:
        m = re.match(r"(center|right|left|justify)$", (node.get("align") or "").strip().lower())
    if m:
        return m.group(1) if m.group(1) in ("center", "right") else ""
    return None


def _al(b):
    a = b.get("align")
    return f"text-align:{a};" if a in ("center", "right") else ""


def _center_all(markup):
    """글 전체를 가운데 정렬 (왼쪽/오른쪽 정렬 → 가운데, 목록은 마커를 안쪽으로, 코드 블록은 왼쪽 유지)"""
    markup = markup.replace(
        ST_LIST, ST_LIST.replace("padding-left:1.4em;", "padding-left:0;list-style-position:inside;"))
    markup = markup.replace(
        '<ol style="margin:0;padding-left:1.3em;line-height:1.9;">',
        '<ol style="margin:0;padding-left:0;list-style-position:inside;line-height:1.9;">')
    return markup.replace("text-align:left;", "text-align:center;").replace("text-align:right;", "text-align:center;")


def _clean_color(style):
    """style 문자열에서 글자색 추출 (검정/흰색 계열은 무시)"""
    m = re.search(r"(?:^|;)\s*color\s*:\s*([^;]+)", style or "")
    if not m:
        return None
    v = m.group(1).strip().lower()
    rgb = None
    h = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", v)
    if h:
        x = h.group(1)
        if len(x) == 3:
            x = "".join(ch * 2 for ch in x)
        rgb = tuple(int(x[i:i + 2], 16) for i in (0, 2, 4))
    else:
        r = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*[\d.]+)?\s*\)", v)
        if r:
            rgb = tuple(min(255, int(g)) for g in r.groups())
            v = "#%02x%02x%02x" % rgb
    if rgb is None:
        return None
    if max(rgb) < 70 or min(rgb) > 235:      # 거의 검정 / 거의 흰색
        return None
    return v


_AD_CLASS_RE = re.compile(
    r"(?:^|[-_])(?:ad|ads|adbox|adwrap|adslot|adunit|adpost|advert|advertisement|banner)(?:$|[-_])"
    r"|adsbygoogle|adpost|naverad|advertisement|googlead|powerlink",
    re.I,
)
_AD_HOSTS = (
    "ad.naver.com", "adcr.naver.com", "adsbygoogle", "doubleclick.net",
    "googleadservices.com", "googlesyndication.com", "adservice.google",
    "ad.daum.net", "criteo.", "taboola.com", "outbrain.com",
)


def _is_ad_href(href):
    h = (href or "").lower()
    return any(x in h for x in _AD_HOSTS)


def _is_ad_node(node, name, classes):
    """광고 영역/광고 링크이면 True (통째로 건너뜀)"""
    if name == "ins":
        return True
    if name == "a" and _is_ad_href(node.get("href")):
        return True
    for c in classes:
        if _AD_CLASS_RE.search(c):
            return True
    nid = node.get("id") or ""
    return bool(nid and _AD_CLASS_RE.search(nid))


_VIDEO_HOST_RE = re.compile(
    r"youtube|youtu\.be|vimeo|tv\.naver|serviceapi\.nmv|nmv\.naver|tv\.kakao|"
    r"dailymotion|twitch\.tv|afreecatv", re.I)


def _is_video_embed(node, classes):
    """동영상 임베드(oEmbed 등)인지 판별"""
    if node.name in ("video", "audio"):
        return True
    if classes & {"se-oembed", "se-module-oembed"}:
        for el in node.find_all(["iframe", "a", "video"]):
            if el.name == "video" or _VIDEO_HOST_RE.search(el.get("src") or el.get("href") or ""):
                return True
    return False


def _to_float(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


def _collect_places(obj, out):
    """JSON 안에서 장소(이름·주소·좌표) 정보를 재귀적으로 찾는다."""
    if isinstance(obj, list):
        for x in obj:
            _collect_places(x, out)
        return
    if not isinstance(obj, dict):
        return
    lat = _to_float(obj.get("latitude", obj.get("lat")))
    lng = _to_float(obj.get("longitude", obj.get("lng", obj.get("lon"))))
    coord_ok = (lat is not None and lng is not None
                and -90 <= lat <= 90 and -180 <= lng <= 180 and (lat or lng))
    name = obj.get("name") or obj.get("placeName") or obj.get("title") or ""
    addr = obj.get("address") or obj.get("roadAddress") or obj.get("roadAddr") or ""
    name = name if isinstance(name, str) else ""
    addr = addr if isinstance(addr, str) else ""
    if coord_ok or (name and addr):
        out.append({
            "name": html.unescape(name).strip(),
            "address": html.unescape(addr).strip(),
            "lat": lat if coord_ok else None,
            "lng": lng if coord_ok else None,
            "place_id": str(obj.get("placeId") or obj.get("placeid") or ""),
            "link": str(obj.get("link") or obj.get("url") or ""),
        })
        return
    for v in obj.values():
        if isinstance(v, (dict, list)):
            _collect_places(v, out)


def _extract_places(node):
    """네이버 지도 컴포넌트에서 장소 목록 추출 (data-linkdata / data-module JSON)"""
    raw = []
    for el in [node] + node.find_all(True):
        for attr in ("data-linkdata", "data-module", "data-module-v2", "data-module-v3"):
            val = el.get(attr)
            if not val or not isinstance(val, str):
                continue
            try:
                _collect_places(json.loads(val, strict=False), raw)
            except Exception:
                pass
    def nk(x):
        return re.sub(r"\s+", "", x or "").lower()

    def ck(p):
        return (round(p["lat"], 4), round(p["lng"], 4)) if p["lat"] is not None else None

    places = []
    for p in raw:      # 같은 장소가 여러 JSON 에 중복 등장하므로 이름/좌표로 합친다
        hit = None
        for q in places:
            if ((nk(p["name"]) and nk(p["name"]) == nk(q["name"]))
                    or (ck(p) and ck(p) == ck(q))
                    or (not nk(p["name"]) and nk(p["address"]) and nk(p["address"]) == nk(q["address"]))):
                hit = q
                break
        if hit is None:
            places.append(dict(p))
            continue
        for k in ("name", "address", "place_id", "link"):
            if not hit[k] and p[k]:
                hit[k] = p[k]
        if hit["lat"] is None and p["lat"] is not None:
            hit["lat"], hit["lng"] = p["lat"], p["lng"]
    # 이름/주소가 비어 있으면 화면에 보이는 글자로 보완
    t = node.select_one(".se-map-title")
    a = node.select_one(".se-map-address")
    for p in places[:1]:
        if not p["name"] and t:
            p["name"] = t.get_text(" ", strip=True)
        if not p["address"] and a:
            p["address"] = a.get_text(" ", strip=True)
    return places[:10]


def _render_map(p, label=""):
    """장소 하나 → 구글 지도 임베드 + 네이버/구글 지도 링크"""
    name, addr = p.get("name", ""), p.get("address", "")
    if p.get("lat") is not None and p.get("lng") is not None:
        plain_q = f'{p["lat"]},{p["lng"]}'
        embed_q = plain_q          # 좌표만 사용 (이름을 넣으면 구글 장소카드가 지도를 가림)
    else:
        plain_q = " ".join(x for x in (addr, name) if x)
        embed_q = plain_q

    link = p.get("link") or ""
    if not re.match(r"https?://", link, re.I):
        if p.get("place_id"):
            link = f'https://map.naver.com/p/entry/place/{p["place_id"]}'
        elif name or addr:
            link = "https://map.naver.com/p/search/" + quote(name or addr)
        else:
            link = ""

    out = [f'<figure style="{ST_MAP_FIG}">']
    if embed_q:
        src = f"https://maps.google.com/maps?q={quote(embed_q, safe=',()')}&z=16&hl=ko&output=embed"
        title = html.escape((name or addr or "지도") + " 지도", quote=True)
        out.append(
            f'<div style="{ST_MAP_BOX}"><iframe src="{html.escape(src, quote=True)}" '
            f'title="{title}" loading="lazy" allowfullscreen '
            f'referrerpolicy="no-referrer-when-downgrade" style="{ST_MAP_FRAME}"></iframe></div>'
        )
    cap = []
    if name or label:
        cap.append("<strong>" + html.escape((f"{label}. " if label else "") + name, quote=False)
                   + "</strong>")
    if addr:
        cap.append(html.escape(addr, quote=False))
    links = []
    if link:
        links.append(f'<a href="{html.escape(link, quote=True)}" target="_blank" '
                     'rel="noopener noreferrer">네이버 지도에서 보기</a>')
    if plain_q:
        g = "https://www.google.com/maps/search/?api=1&query=" + quote(plain_q, safe=",")
        links.append(f'<a href="{html.escape(g, quote=True)}" target="_blank" '
                     'rel="noopener noreferrer">구글 지도에서 보기</a>')
    if links:
        cap.append(" · ".join(links))
    if cap:
        out.append(f'<figcaption style="{ST_MAP_CAP}">{"<br>".join(cap)}</figcaption>')
    out.append("</figure>")
    return "".join(out)


# ---------------------------------------------------------------
# 네이버 DOM → 줄/블록 스트림
# ---------------------------------------------------------------
class _Walker:
    """
    네이버 본문 DOM 을 순회해 다음 항목의 나열로 바꾼다.
      ("line", 인라인HTML) / ("blank",) / ("heading", "big"|"bold", HTML) / ("img", dict)
      ("hr",) / ("quote", [HTML...]) / ("table", rows) / ("code", text)
      ("link", url, title, desc) / ("list", ordered, [HTML...])
    """

    SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "button",
                 "input", "select", "textarea", "form", "object", "embed", "ins", "video", "audio", "source"}
    SKIP_CLASSES = {"se-sticker", "se-module-sticker", "se-sticker-image",
                    "se-material", "se-banner", "se-caption"}
    TITLE_CLASSES = {"se-documentTitle", "se-module-documentTitle"}
    STRIP_CLASSES = {"se-imageStrip", "se-imageGroup"}
    MAP_CLASSES = {"se-placesMap", "se-map", "se-module-map", "se-module-placesMap"}
    VIDEO_CLASSES = {"se-video", "se-module-video"}
    BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main",
                  "aside", "figure", "figcaption", "center", "li", "dl", "dt",
                  "dd", "address", "nav", "details", "summary", "tbody", "thead"}

    # simple 모드(표 셀·인용·소제목 안쪽을 읽을 때)에서 줄바꿈 단위로만 취급하는 태그
    SIMPLE_BLOCKS = BLOCK_TAGS | {"h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
                                  "table", "tr", "td", "th", "blockquote", "pre"}

    def __init__(self, br_as_space=False, simple=False, img_width=966, maps=True, cover=True):
        self.items = []
        self.buf = []
        self.stack = []
        self.has_text = False
        self.br_as_space = br_as_space
        self.simple = simple
        self.img_width = img_width
        self.maps = maps
        self.cover = cover
        self.cur_align = ""
        self.last_align = ""

    # ----- 줄 버퍼 -----
    def add_text(self, s):
        s = s.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
        s = re.sub(r"\s+", " ", s)
        if not self.has_text:
            s = s.lstrip()
        if not s:
            return
        self.buf.append(html.escape(s, quote=False))
        self.has_text = True

    def flush(self, force=False):
        emitted = False
        if self.has_text:
            line = "".join(self.buf) + "".join(
                f"</{n}>" for n, _ in reversed(self.stack)
            )
            line = _clean_line(line)
            if plain_text(line):
                if self.cur_align != self.last_align:
                    self.items.append(("align", self.cur_align))
                    self.last_align = self.cur_align
                self.items.append(("line", line))
                emitted = True
        if not emitted and force:
            self.items.append(("blank",))
        self.buf = [opener for _, opener in self.stack]
        self.has_text = False

    def _open(self, name, tag):
        self.buf.append(tag)
        self.stack.append((name, tag))

    def _close(self, name):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == name:
                del self.stack[i]
                break
        self.buf.append(f"</{name}>")

    # ----- 하위 문서를 따로 읽기 -----
    @staticmethod
    def _sub_lines(node, br_as_space=False):
        w = _Walker(br_as_space=br_as_space, simple=True)
        for c in node.children:
            w.walk(c)
        w.flush()
        return [it[1] for it in w.items if it[0] == "line"]

    # ----- 순회 -----
    def walk(self, node):
        if isinstance(node, NavigableString):
            if type(node) is NavigableString:
                self.add_text(str(node))
            return
        if not isinstance(node, Tag):
            return

        name = (node.name or "").lower()
        classes = set(node.get("class") or [])

        if name in self.SKIP_TAGS or classes & self.SKIP_CLASSES:
            return
        if _is_ad_node(node, name, classes):      # 광고 제외
            return
        if classes & self.MAP_CLASSES:             # 지도
            if not self.simple and self.maps:
                self._map(node)
            return
        if classes & self.VIDEO_CLASSES or _is_video_embed(node, classes):
            if not self.simple:       # 동영상은 흔적(문구·링크·썸네일) 없이 통째로 제외
                self.flush()
            return

        if classes & self.TITLE_CLASSES:           # 글 맨 위 커버(대표 이미지)
            if not self.simple and self.cover:
                self._cover(node)
            return
        if classes & self.STRIP_CLASSES:           # 나란히 놓인 이미지(콜라주)
            if not self.simple:
                self._strip(node)
            return

        if name == "br":
            if self.br_as_space:
                self.add_text(" ")
            else:
                self.flush(force=True)
            return
        if name == "img":
            self._image(node)
            return
        if name == "hr":
            if not self.simple:
                self.flush()
                self.items.append(("hr",))
            return

        if not self.simple:
            if any("sectionTitle" in c for c in classes):
                self._heading(node, "big")
                return
            if "se-quotation" in classes or name == "blockquote":
                self._quote(node)
                return
            if "se-horizontalLine" in classes:
                self.flush()
                self.items.append(("hr",))
                return
            if "se-oglink" in classes:
                self._oglink(node)
                return
            if "se-table" in classes or name == "table":
                self._table(node)
                return
            if "se-code" in classes or name == "pre":
                self._code(node)
                return
            if "se-image" in classes:
                self._image_component(node)
                return
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self._heading(node, "big" if name in ("h1", "h2") else "bold")
                return
            if name in ("ul", "ol"):
                self._list(node, name == "ol")
                return

        if name in (self.SIMPLE_BLOCKS if self.simple else self.BLOCK_TAGS):
            self.flush()
            prev_align = self.cur_align
            al = _align_of(classes, node)
            if al is not None:
                self.cur_align = al
            before = len(self.items)
            for c in node.children:
                self.walk(c)
            self.flush()
            self.cur_align = prev_align
            if len(self.items) == before:
                self.items.append(("blank",))   # 빈 문단 = 문단 구분
            return

        # 인라인 요소
        tags = self._inline_tags(node, name, classes)
        for n, t in tags:
            self._open(n, t)
        for c in node.children:
            self.walk(c)
        for n, _ in reversed(tags):
            self._close(n)

    def _inline_tags(self, node, name, classes):
        tags = []
        style = (node.get("style") or "").lower()

        if name in ("b", "strong"):
            tags.append(("strong", "<strong>"))
        elif name in ("i", "em"):
            tags.append(("em", "<em>"))
        elif name == "u":
            tags.append(("u", "<u>"))
        elif name in ("s", "strike", "del"):
            tags.append(("s", "<s>"))
        elif name in ("sup", "sub", "mark", "small", "code"):
            tags.append((name, f"<{name}>"))
        elif name == "a":
            href = (node.get("href") or "").strip()
            if href.startswith("//"):
                href = "https:" + href
            if re.match(r"https?://", href, re.I):
                tags.append((
                    "a",
                    f'<a href="{html.escape(href, quote=True)}" '
                    'target="_blank" rel="noopener noreferrer">',
                ))

        big = 0
        for c in classes:
            m = re.fullmatch(r"se-fs-fs(\d+)", c)
            if m:
                big = int(m.group(1))

        if re.search(r"font-weight\s*:\s*(bold|[6-9]00)", style):
            tags.append(("strong", "<strong>"))
        if re.search(r"font-style\s*:\s*italic", style):
            tags.append(("em", "<em>"))
        if "underline" in style:
            tags.append(("u", "<u>"))
        if "line-through" in style:
            tags.append(("s", "<s>"))
        if big >= 19:  # 큰 글씨는 소제목 후보로 표시
            tags = [t for t in tags if t[0] != "strong"]
            tags.append(("strong", '<strong data-big="1">'))

        col = _clean_color(style)
        if col:
            tags.append(("span", f'<span style="color:{col}">'))

        # 이미 열려 있는 같은 서식은 중복해서 열지 않는다
        out, seen = [], {n for n, _ in self.stack}
        for n, t in tags:
            if n in ("strong", "em", "u", "s") and n in seen:
                continue
            seen.add(n)
            out.append((n, t))
        return out

    # ----- 블록 요소들 -----
    def _heading(self, node, kind):
        text = " ".join(self._sub_lines(node, br_as_space=True))
        text = _strip_strong(text).strip()
        if plain_text(text):
            self.flush()
            self.items.append(("heading", kind, text))

    def _quote(self, node):
        lines = self._sub_lines(node)
        if lines:
            self.flush()
            self.items.append(("quote", lines))

    def _list(self, node, ordered):
        items = []
        for li in node.find_all("li", recursive=False):
            text = " ".join(self._sub_lines(li, br_as_space=True))
            if plain_text(text):
                items.append(text)
        if items:
            self.flush()
            self.items.append(("list", ordered, items))

    def _table(self, node):
        rows = []
        for tr in node.find_all("tr"):
            cells = []
            for td in tr.find_all(["td", "th"], recursive=False):
                text = " ".join(self._sub_lines(td, br_as_space=True))
                cells.append((
                    td.name.lower(), text,
                    _to_int(td.get("colspan")) or 1,
                    _to_int(td.get("rowspan")) or 1,
                ))
            if cells and any(plain_text(c[1]) for c in cells):
                rows.append(cells)
        if rows:
            self.flush()
            self.items.append(("table", rows))

    def _code(self, node):
        text = node.get_text()
        text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
        if text.strip():
            self.flush()
            self.items.append(("code", text))

    def _oglink(self, node):
        a = node.select_one("a[href]")
        url = (a.get("href") if a else "") or ""
        if not re.match(r"https?://", url, re.I):
            return
        t = node.select_one(".se-oglink-title")
        d = node.select_one(".se-oglink-summary")
        title = t.get_text(" ", strip=True) if t else url
        desc = d.get_text(" ", strip=True) if d else ""
        thumb = ""
        th = node.select_one("img")
        if th:
            thumb = (th.get("data-lazy-src") or th.get("src") or "").strip()
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            if thumb.startswith("http://"):
                thumb = "https://" + thumb[7:]
            if not thumb.startswith("https://"):
                thumb = ""
        self.flush()
        self.items.append(("link", url, title, desc, thumb))

    def _cover(self, node):
        """글 맨 위 커버: <img> 또는 CSS 배경 이미지를 첫 이미지로 가져온다 (제목 글자는 제외)"""
        url = ""
        for el in [node] + node.find_all(True):
            if el.name == "img":
                url = (el.get("data-lazy-src") or el.get("src") or "").strip()
            if not url:
                m = re.search(r"url\(\s*['\"]?([^'\")]+)", el.get("style") or "")
                if m:
                    url = html.unescape(m.group(1)).strip()
            if url:
                break
        if not url:
            return
        t = node.select_one(".se-title-text")
        alt = t.get_text(" ", strip=True) if t else ""
        self._image(Tag(name="img", attrs={"src": url, "alt": alt}))

    def _strip(self, node):
        """나란히 놓인 이미지 묶음 → 가로 한 줄(최대 3장)로"""
        n0 = len(self.items)
        self.flush()
        for img in node.find_all("img"):
            self._image(img)
        self.flush()
        imgs = [it[1] for it in self.items[n0:] if it[0] == "img"]
        if len(imgs) >= 2:
            del self.items[n0:]
            for i in range(0, len(imgs), 3):
                chunk = imgs[i:i + 3]
                if len(chunk) >= 2:
                    self.items.append(("imgrow", chunk))
                else:
                    self.items.append(("img", chunk[0]))

    def _map(self, node):
        places = _extract_places(node)
        self.flush()
        if places:
            self.items.append(("map", places))
            return
        # 좌표/주소를 못 읽었으면 보이는 글자와 링크라도 남긴다
        a = node.select_one("a[href]")
        href = ((a.get("href") if a else "") or "").strip()
        t = node.select_one(".se-map-title")
        ad = node.select_one(".se-map-address")
        name = t.get_text(" ", strip=True) if t else ""
        addr = ad.get_text(" ", strip=True) if ad else ""
        if name or addr or re.match(r"https?://", href, re.I):
            self.items.append(("map", [{
                "name": name, "address": addr, "lat": None, "lng": None,
                "place_id": "", "link": href if re.match(r"https?://", href, re.I) else "",
            }]))

    def _image_component(self, node):
        cap = node.select_one(".se-caption")
        caption = cap.get_text(" ", strip=True).replace("\u200b", "") if cap else ""
        for i, img in enumerate(node.find_all("img")):
            self._image(img, caption if i == 0 else "")

    def _image(self, img, caption=""):
        classes = set(img.get("class") or [])
        if classes & {"se-sticker-image", "se-inline-image"}:
            return
        src = (img.get("data-lazy-src") or img.get("data-src") or img.get("src") or "").strip()
        if src.startswith("//"):
            src = "https:" + src
        if not src or src.startswith("data:") or "/sticker/" in src:
            return

        width = _to_int(img.get("data-width"))
        height = _to_int(img.get("data-height"))
        if width and width < 50:  # 아이콘/구분선 이미지
            return

        if src.startswith("http://"):
            src = "https://" + src[7:]
        base = src.split("?")[0]
        if "pstatic.net" in base:
            if base.lower().endswith(".gif") or self.img_width <= 0:
                src = base                      # 움짤/원본 선택 시 원본 유지
            else:
                tw = self.img_width
                src = base + f"?type=w{tw}"
                if width and height and width > tw:
                    height = round(height * tw / width)
                    width = tw

        self.flush()
        self.items.append((
            "img",
            {
                "src": src,
                "alt": (img.get("alt") or "").strip(),
                "caption": caption.strip(),
                "w": width,
                "h": height,
            },
        ))


# ---------------------------------------------------------------
# 줄 분류 규칙
# ---------------------------------------------------------------
_RE_Q = re.compile(r"^((?:<[^>]+>)*)\s*(?:Q|질문)\s*\d{0,2}\s*[.:)：]\s*", re.I)
_RE_A = re.compile(r"^((?:<[^>]+>)*)\s*(?:A|답변|답)\s*\d{0,2}\s*[.:)：]\s*", re.I)
_RE_BULLET = re.compile(
    r"^((?:<[^>]+>)*)\s*(?:[-–—*]\s+|\*(?=[^\s*])|[•·●○▪▫■□◆◇▶▷►✔✅☑✓]\ufe0f?\s*)"
)
_RE_NUMBER = re.compile(r"^((?:<[^>]+>)*)\s*(?:\d{1,2}\s*[.)]\s+|[①-⑳]\s*)")
_RE_KV = re.compile(r"^[^:：]{1,15}[:：]\s+\S")
_RE_BOLD_LINE = re.compile(r'<strong( data-big="1")?>((?:(?!</?strong).)*)</strong>')


def _heading_kind(line, plain):
    """굵은 글씨(또는 큰 글씨)로만 이루어진 짧은 한 줄이면 소제목 후보"""
    m = _RE_BOLD_LINE.fullmatch(line)
    if not m:
        return None
    if not (2 <= len(plain) <= 45):
        return None
    if re.search(r"[.!?。！？~…,]$|[ㅎㅋㅠㅜ]+$", plain):
        return None
    return "big" if m.group(1) else "bold"


def _group_to_blocks(lines, opts, state):
    """빈 줄로 구분된 연속 줄 묶음 → 블록(소제목/목록/문단) 목록"""
    kinds = []
    lines = [seg for l in lines for seg in _split_star_items(l)]   # '*A *B' → 항목별 줄
    hk_all = [_heading_kind(l, plain_text(l)) for l in lines]
    lines = _prejoin(lines, hk_all, opts["auto_heading"], state["faq"])
    hk_all = [_heading_kind(l, plain_text(l)) for l in lines]
    for idx, line in enumerate(lines):
        plain = plain_text(line)
        if not plain:
            continue

        m = _RE_Q.match(line)
        if m:
            q = _strip_strong(m.group(1) + line[m.end():]).strip()
            if plain_text(q):
                state["faq"] = True
                kinds.append({"k": "q", "html": q, "orig": line})
                continue

        if state["faq"]:
            m = _RE_A.match(line)
            if m:
                line = m.group(1) + line[m.end():]
                plain = plain_text(line)
                if not plain:
                    continue

        if opts["auto_heading"]:
            hk = hk_all[idx]
            # 굵은 줄이 연달아 나오면 소제목이 아니라 '굵게 쓴 본문'이다
            if hk == "bold" and ((idx > 0 and hk_all[idx - 1])
                                 or (idx + 1 < len(lines) and hk_all[idx + 1])):
                hk = None
            if hk:
                state["faq"] = False
                kinds.append({"k": "h", "html": _strip_strong(line), "lvl": hk, "orig": line})
                continue

        m = _RE_NUMBER.match(line)
        if m:
            kinds.append({"k": "li", "ordered": True, "html": m.group(1) + line[m.end():], "orig": line})
            continue
        m = _RE_BULLET.match(line)
        if m:
            kinds.append({"k": "li", "ordered": False, "html": m.group(1) + line[m.end():], "orig": line})
            continue
        if _RE_KV.match(plain) and not re.search(r"[.!?。]$", plain):
            kinds.append({"k": "kv", "ordered": False, "html": line, "orig": line})
            continue

        kinds.append({"k": "p", "html": line, "orig": line})

    # (한 줄뿐인 목록 후보는 _finalize_blocks 에서 앞뒤 블록과 합쳐 본 뒤 문장으로 되돌린다)
    blocks = []
    i, n = 0, len(kinds)
    while i < n:
        k = kinds[i]["k"]
        j = i
        while (j < n and kinds[j]["k"] == k
               and kinds[j].get("ordered") == kinds[i].get("ordered")):
            j += 1
        run = kinds[i:j]
        if k == "p":
            run_html = [r["html"] for r in run]
            if opts.get("keep_original_paragraphs"):
                paras = run_html                                      # 원문 문단 그대로
            else:
                paras, seg = [], []
                for h in run_html:          # 라벨 줄('💡 꿀 Tip')은 본문과 섞지 않고 따로 둔다
                    if _is_label_line(h):
                        if seg:
                            paras.extend(pack_prose(seg, opts["para_max_chars"], opts["para_max_sentences"]))
                            seg = []
                        paras.append(h)
                    else:
                        seg.append(h)
                if seg:
                    paras.extend(pack_prose(seg, opts["para_max_chars"], opts["para_max_sentences"]))
            for para in paras:
                blocks.append({"kind": "p", "html": para})
        elif k in ("li", "kv"):
            blocks.append({"kind": "list", "ordered": bool(run[0].get("ordered")),
                           "items": [r["html"] for r in run],
                           "origs": [r["orig"] for r in run], "from_text": True})
        elif k == "h":
            for r in run:
                blocks.append({"kind": "h", "lvl": r["lvl"], "html": r["html"]})
        elif k == "q":
            for r in run:
                blocks.append({"kind": "h", "lvl": "faq", "html": r["html"], "faq": True})
        i = j
    return blocks


def _items_to_blocks(items, opts):
    blocks, group = [], []
    state = {"faq": False}
    cur_align = [""]

    # 굵은 글씨가 대부분인 글은 '굵은 줄 → 소제목' 자동 변환이 오히려 글을 망가뜨린다
    lines_all = [it[1] for it in items if it[0] == "line"]
    if opts.get("auto_heading") and len(lines_all) >= 10:
        nb = sum(1 for l in lines_all if _RE_BOLD_LINE.fullmatch(l))
        if nb / len(lines_all) >= 0.4:
            opts = dict(opts, auto_heading=False)
            log.info("  굵은 글씨가 대부분이라 소제목 자동변환을 건너뜁니다")

    def flush_group():
        if group:
            new = _group_to_blocks(list(group), opts, state)
            if cur_align[0]:
                for b in new:
                    if b["kind"] in ("p", "h"):
                        b["align"] = cur_align[0]
            blocks.extend(new)
            group.clear()

    for it in items:
        kind = it[0]
        if kind == "line":
            group.append(it[1])
        elif kind == "blank":
            flush_group()
        elif kind == "align":
            flush_group()
            cur_align[0] = it[1]
        else:
            flush_group()
            if kind == "heading":
                state["faq"] = False
                blocks.append({"kind": "h", "lvl": it[1], "html": it[2]})
            elif kind == "img":
                blocks.append(dict(it[1], kind="img"))
            elif kind == "hr":
                blocks.append({"kind": "hr"})
            elif kind == "quote":
                blocks.append({"kind": "quote", "lines": it[1]})
            elif kind == "table":
                blocks.append({"kind": "table", "rows": it[1]})
            elif kind == "code":
                blocks.append({"kind": "code", "text": it[1]})
            elif kind == "imgrow":
                blocks.append({"kind": "imgrow", "imgs": it[1]})
            elif kind == "map":
                n = len(it[1])
                for i, place in enumerate(it[1]):
                    blocks.append({"kind": "map", "place": place,
                                   "label": chr(65 + i) if n > 1 and i < 26 else ""})
            elif kind == "link":
                blocks.append({"kind": "link", "url": it[1], "title": it[2], "desc": it[3],
                               "thumb": it[4] if len(it) > 4 else ""})
            elif kind == "list":
                blocks.append({"kind": "list", "ordered": it[1], "items": it[2],
                               "origs": list(it[2]), "from_text": False})
    flush_group()
    return blocks


def _finalize_blocks(blocks):
    """목록 병합 · 소제목 정리 · 소제목 레벨(h2/h3) 결정"""
    merged = []
    for b in blocks:
        if (b["kind"] == "list" and merged and merged[-1]["kind"] == "list"
                and merged[-1]["ordered"] == b["ordered"]):
            merged[-1]["items"].extend(b["items"])
            merged[-1]["origs"].extend(b["origs"])
            merged[-1]["from_text"] = merged[-1]["from_text"] and b["from_text"]
        else:
            merged.append(b)
    blocks = merged

    # 글자로만 추정한 목록이 항목 1개뿐이면 일반 문단으로 되돌린다
    for b in blocks:
        if b["kind"] == "list" and b["from_text"] and len(b["items"]) == 1:
            b.update(kind="p", html=b["origs"][0])

    # 뒤따르는 내용이 없는 소제목은 일반 문단으로 되돌린다
    def has_content_after(idx):
        return any(x["kind"] not in ("hr",) for x in blocks[idx + 1:])

    for idx, b in enumerate(blocks):
        if b["kind"] == "h" and not has_content_after(idx):
            b.update(kind="p", html=b["html"])

    # 소제목 레벨: 큰 글씨/소제목 컴포넌트가 있으면 그것이 h2, 굵은 줄은 h3
    heads = [b for b in blocks if b["kind"] == "h"]
    has_big = any(b["lvl"] == "big" for b in heads)
    has_non_faq = any(b["lvl"] != "faq" for b in heads)
    for b in heads:
        if b["lvl"] == "big":
            b["level"] = 2
        elif b["lvl"] == "bold":
            b["level"] = 3 if has_big else 2
        else:  # faq
            b["level"] = 3 if has_non_faq else 2
    return blocks


# ---------------------------------------------------------------
# 블록 → HTML
# ---------------------------------------------------------------
_BAD_ALT_RE = re.compile(
    r"^(?:image|img|photo|picture|이미지|사진)?\s*\d*$|\.(?:jpe?g|png|gif|webp)$", re.I
)


def _render_table(rows):
    has_th = any(c[0] == "th" for r in rows for c in r)
    header_first = (
        not has_th and len(rows) >= 2
        and all(plain_text(c[1]) and len(plain_text(c[1])) <= 30 for c in rows[0])
    )

    def cell(tag, content, colspan, rowspan, header):
        attrs = ""
        if colspan > 1:
            attrs += f' colspan="{colspan}"'
        if rowspan > 1:
            attrs += f' rowspan="{rowspan}"'
        if header:
            return f'<th scope="col"{attrs} style="{ST_TH}">{content}</th>'
        if tag == "th":
            return f'<th{attrs} style="{ST_TH}">{content}</th>'
        return f'<td{attrs} style="{ST_CELL}">{content}</td>'

    out = [f'<div style="{ST_TABLE_WRAP}"><table style="{ST_TABLE}">']
    body_rows = rows
    if header_first:
        out.append(
            "<thead><tr>" + "".join(cell(*c, True) for c in rows[0]) + "</tr></thead>"
        )
        body_rows = rows[1:]
    out.append("<tbody>")
    for row in body_rows:
        out.append("<tr>" + "".join(cell(*c, False) for c in row) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _make_description(blocks):
    desc = ""
    for b in blocks:
        if b["kind"] == "p":
            desc = (desc + " " + plain_text(b["html"])).strip()
            if len(desc) >= 90:
                break
    if len(desc) > 155:
        cut = desc[:155]
        m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        desc = cut[:m + 1] if m >= 80 else cut[:152].rstrip() + "…"
    return desc


def _collect_faq(blocks):
    faqs = []
    for idx, b in enumerate(blocks):
        if b["kind"] == "h" and b.get("faq"):
            answer = []
            for nxt in blocks[idx + 1:]:
                if nxt["kind"] == "p":
                    answer.append(plain_text(nxt["html"]))
                elif nxt["kind"] == "list":
                    answer.extend(plain_text(x) for x in nxt["items"])
                else:
                    break
            q, a = plain_text(b["html"]), " ".join(answer).strip()
            if q and a:
                faqs.append((q, a[:1000]))
    return faqs


def _render_blocks(blocks, title, opts):
    parts, headings = [], []
    head_no = 0
    img_no = 0
    last_heading = ""
    wrap_w = _to_int(opts.get("wrap_chars")) or 0

    def sw(markup):
        return soft_wrap(markup, wrap_w)

    for b in blocks:
        k = b["kind"]
        if k == "p":
            parts.append(f'<p style="{ST_P}{_al(b)}">{sw(b["html"])}</p>')

        elif k == "h":
            lv = b["level"]
            head_no += 1
            hid = f"sec{head_no}"
            last_heading = plain_text(b["html"])
            if lv == 2 or not b.get("faq"):   # 목차: h2 + FAQ 가 아닌 h3
                headings.append((hid, last_heading, lv))
            style = ST_H2 if lv == 2 else ST_H3
            parts.append(f'<h{lv} id="{hid}" style="{style}{_al(b)}">{sw(b["html"])}</h{lv}>')

        elif k == "list":
            tag = "ol" if b["ordered"] else "ul"
            lis = "".join(f'<li style="{ST_LI}">{sw(x)}</li>' for x in b["items"])
            parts.append(f'<{tag} style="{ST_LIST}">{lis}</{tag}>')

        elif k in ("img", "imgrow"):
            imgs = [b] if k == "img" else b["imgs"]
            made = []
            for im in imgs:
                img_no += 1
                alt = im["alt"]
                if not alt or _BAD_ALT_RE.search(alt) or len(alt) < 2:
                    alt = im["caption"] or (
                        f"{last_heading} 사진 {img_no}" if last_heading else f"{title} 사진 {img_no}"
                    )
                size = ""
                if im.get("w") and im.get("h"):
                    size = f' width="{im["w"]}" height="{im["h"]}"'
                loading = "" if img_no == 1 else ' loading="lazy"'
                # referrerpolicy: 네이버 이미지 서버의 외부 사이트 핫링크 차단(깨진 이미지) 회피
                tag = (
                    f'<img src="{html.escape(im["src"], quote=True)}" '
                    f'alt="{html.escape(alt, quote=True)}"{size}{loading} decoding="async" '
                    f'referrerpolicy="no-referrer" '
                    f'style="{ST_IMG if k == "img" else ST_IMG_ROW}">'
                )
                made.append((im, tag))
            if k == "img":
                im, tag = made[0]
                fig = f'<figure style="{ST_FIGURE}">{tag}'
                if im["caption"]:
                    fig += f'<figcaption style="{ST_CAP}">{sw(html.escape(im["caption"], quote=False))}</figcaption>'
                parts.append(fig + "</figure>")
            else:
                cells = ""
                for im, tag in made:
                    cap = ""
                    if im["caption"]:
                        cap = f'<figcaption style="{ST_CAP}">{html.escape(im["caption"], quote=False)}</figcaption>'
                    cells += f'<figure style="{ST_ROW_CELL}">{tag}{cap}</figure>'
                parts.append(f'<div style="{ST_ROW}">{cells}</div>')

        elif k == "quote":
            inner = "".join(f'<p style="{ST_QUOTE_P}">{x}</p>' for x in b["lines"])
            parts.append(f'<blockquote style="{ST_QUOTE}">{inner}</blockquote>')

        elif k == "table":
            parts.append(_render_table(b["rows"]))

        elif k == "code":
            body = html.escape(b["text"], quote=False).replace("\r", "").replace("\n", "&#10;")
            parts.append(f'<pre style="{ST_PRE}"><code>{body}</code></pre>')

        elif k == "link":
            href = html.escape(b["url"], quote=True)
            thumb = ""
            if b.get("thumb"):
                thumb = (
                    f'<a href="{href}" target="_blank" rel="noopener noreferrer" '
                    'style="flex:0 0 96px;display:block;">'
                    f'<img src="{html.escape(b["thumb"], quote=True)}" alt="" loading="lazy" '
                    'referrerpolicy="no-referrer" '
                    'style="display:block;width:96px;height:100%;min-height:72px;object-fit:cover;"></a>'
                )
            desc = ""
            if b["desc"]:
                desc = ('<br><span style="font-size:.88em;color:#777;">'
                        f'{html.escape(b["desc"], quote=False)}</span>')
            parts.append(
                f'<div style="{ST_LINKCARD}">{thumb}<div style="{ST_LINKCARD_TXT}">'
                f'<a href="{href}" target="_blank" rel="noopener noreferrer">'
                f'<strong>{html.escape(b["title"], quote=False)}</strong></a>{desc}</div></div>'
            )

        elif k == "map":
            parts.append(_render_map(b["place"], b.get("label", "")))

        elif k == "hr":
            parts.append(f'<hr style="{ST_HR}">')

    # 목차 (소제목이 3개 이상일 때) — 서두 문단 바로 뒤에 둔다
    toc = ""
    if opts["add_toc"] and 3 <= len(headings) <= 25:
        li = "".join(
            f'<li style="margin:0{" 0 0 1em" if lv == 3 else ""};">'
            f'<a href="#{hid}" style="text-decoration:none;">'
            f'{html.escape(text, quote=False)}</a></li>'
            for hid, text, lv in headings
        )
        toc = (
            f'<nav aria-label="목차" style="{ST_TOC}">'
            '<p style="margin:0 0 .4em;font-weight:700;">목차</p>'
            f'<ol style="margin:0;padding-left:1.3em;line-height:1.9;">{li}</ol></nav>'
        )
        # 서두(첫 2문단)는 목차보다 먼저 보이게 둔다 (GEO: 답을 먼저, 구조는 그 다음)
        first_h = next(i for i, b in enumerate(blocks) if b["kind"] == "h")
        at, seen_p = first_h, 0
        for i, b in enumerate(blocks[:first_h]):
            if b["kind"] == "p":
                seen_p += 1
                if seen_p == 2:
                    at = i + 1
                    break
        parts.insert(at, toc)

    return "".join(parts), head_no


def _clean_content(container, blog_id, log_no, title, add_date, cfg, tags=None):
    """
    네이버 본문 DOM → 블로거용 HTML.
    반환: (HTML 문자열, 메타 dict)
    """
    opts = {
        k: cfg.get(k, DEFAULT_CONFIG[k])
        for k in ("para_max_chars", "para_max_sentences", "pc_line_chars", "wrap_chars",
                  "auto_heading", "add_toc", "add_json_ld",
                  "keep_original_paragraphs")
    }
    opts["src_url"] = f"https://blog.naver.com/{blog_id}/{log_no}"
    tags = list(tags or [])

    walker = _Walker(
        img_width=_to_int(cfg.get("image_width", DEFAULT_CONFIG["image_width"])) or 0,
        maps=bool(cfg.get("import_maps", True)),
        cover=bool(cfg.get("import_cover", True)),
    )
    for child in container.children:
        walker.walk(child)
    walker.flush()

    # 본문에 섞여 들어온 '#취소#확인' (네이버 화면 버튼 글자) 제거
    cleaned = []
    for it in walker.items:
        if it[0] == "line":
            t = _strip_junk_tags(it[1])
            if not plain_text(t):
                continue
            it = ("line", t)
        cleaned.append(it)
    walker.items = cleaned

    blocks = _finalize_blocks(_items_to_blocks(walker.items, opts))
    inner, n_headings = _render_blocks(blocks, title or "", opts)

    tags_html = ""
    if tags and cfg.get("hashtags_in_body", True):
        tags_html = f'<p style="{ST_TAGS}">' + "".join(
            f'<span style="{ST_TAG}">#{html.escape(t, quote=False)}</span>' for t in tags
        ) + "</p>"

    body = (
        f'<div lang="ko" style="{_WRAP.format(em=opts["pc_line_chars"])}">'
        f"{inner}{tags_html}</div>"
    )

    if cfg.get("center_all", True):
        body = _center_all(body)

    faqs = _collect_faq(blocks)
    if opts["add_json_ld"] and len(faqs) >= 2:
        data = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": q,
                 "acceptedAnswer": {"@type": "Answer", "text": a}}
                for q, a in faqs
            ],
        }
        ld = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        body += f'<script type="application/ld+json">{ld}</script>'

    body = re.sub(r"[\r\n\t]+", " ", body)   # 블로거 '줄바꿈 변환'으로 <br>이 끼어드는 것 방지

    meta = {
        "description": _make_description(blocks),
        "headings": n_headings,
        "paragraphs": sum(1 for b in blocks if b["kind"] == "p"),
        "images": sum(1 for b in blocks if b["kind"] == "img")
                  + sum(len(b["imgs"]) for b in blocks if b["kind"] == "imgrow"),
        "faq": len(faqs),
        "maps": sum(1 for b in blocks if b["kind"] == "map"),
        "links": len(re.findall(r"<a\s", inner)),
        "tags": tags,
    }
    return body, meta


def _clean_tag(t):
    t = html.unescape(t or "").strip().lstrip("#").strip()
    t = re.sub(r"[,<>\r\n\t]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _extract_hashtags(soup, blog_id, log_no):
    """글 하단 해시태그 목록 (화면 → 실패 시 태그 API)"""
    tags, seen = [], set()

    def add(t):
        t = _clean_tag(t)
        if t in _UI_JUNK_TAGS:            # '#취소 #확인' 버튼 글자 제외
            return
        if t and t.lower() not in seen and len(tags) < 30:
            seen.add(t.lower())
            tags.append(t)

    for sel in ("div.post_tag a", ".wrap_tag a", "#tagList a", "a.itemTagfont",
                ".blog_tag a", ".se-hashtag a"):
        for a in soup.select(sel):
            add(a.get_text(" ", strip=True))
        if tags:
            break

    if not tags:
        try:
            r = SESSION.get(
                "https://blog.naver.com/BlogTagListInfo.naver",
                params={"blogId": blog_id, "logNoList": log_no, "logType": "mylog"},
                timeout=15,
            )
            r.raise_for_status()
            for raw in re.findall(r'"tagName"\s*:\s*"(.*?)"', r.text, re.S):
                for part in unquote_plus(raw.replace("\\'", "'")).split(","):
                    add(part)
        except Exception as e:
            log.info("  해시태그 조회 실패(무시): %s", e)
    return tags


def build_labels(base_labels, tags, use_tags):
    """블로거 라벨 만들기 (중복 제거, 전체 길이 제한)
    한글은 UTF-8 로 글자당 3바이트라서 '글자 수'가 아니라 '바이트 수(쉼표 포함)'로 센다."""
    labels, seen, total = [], set(), 0
    for raw in list(base_labels or []) + (list(tags or []) if use_tags else []):
        l = _clean_tag(raw)[:50]
        cost = len(l.encode("utf-8")) + 1          # +1: 라벨 사이 쉼표
        if not l or l.lower() in seen or total + cost > 200:
            continue
        seen.add(l.lower())
        labels.append(l)
        total += cost
    return labels


def fetch_post_content(blog_id, log_no, cfg, title="", add_date=""):
    """글 하나의 본문을 가져와 변환. 반환: (HTML, 메타 dict)"""
    url = "https://blog.naver.com/PostView.naver"
    params = {
        "blogId": blog_id,
        "logNo": log_no,
        "redirect": "Dlog",
        "widgetTypeCall": "true",
        "directAccess": "false",
    }
    resp = SESSION.get(url, params=params, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    container = (
        soup.select_one("div.se-main-container")      # 스마트에디터 ONE
        or soup.select_one("div#postViewArea")        # 구 에디터
        or soup.select_one("div.se_component_wrap")   # 스마트에디터 2
    )
    if container is None:
        raise RuntimeError("본문 영역을 찾지 못했습니다 (비공개/삭제된 글일 수 있음)")
    tags = _extract_hashtags(soup, blog_id, log_no)
    return _clean_content(container, blog_id, log_no, title, add_date, cfg, tags)


def parse_add_date(text):
    """'2024. 5. 3.' → ISO8601(KST). 상대시간('3시간 전') 등은 None"""
    m = re.match(r"\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", text or "")
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        # 블로그 시간대(한국/미국 등)와 무관하게 같은 날짜로 보이도록 UTC 10:00 으로 지정
        # (예전 12:00 KST 는 미국 시간대 블로거에서 하루 전 날짜로 표시되었음)
        return datetime(y, mo, d, 10, 0, 0, tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def list_posts(blog_id):
    return json.dumps(fetch_post_list(blog_id), ensure_ascii=False)


def convert_post(cfg_json, log_no, title, add_date):
    """글 하나 변환 → JSON 문자열 (JS 쪽에서 블로거로 발행)"""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(cfg_json))
    content, meta = fetch_post_content(cfg["naver_blog_id"], log_no, cfg, title, add_date)
    labels = build_labels(cfg["labels"], meta["tags"], cfg.get("hashtags_as_labels", True))
    return json.dumps({
        "content": content,
        "labels": labels,
        "published": parse_add_date(add_date),
        "meta": {k: meta[k] for k in ("headings", "paragraphs", "images", "maps", "links", "faq", "description") if k in meta},
        "tags": meta.get("tags", []),
    }, ensure_ascii=False)
