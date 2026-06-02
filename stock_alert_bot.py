"""
미국지수 ETF 보유자용 텔레그램 알림 봇 (GitHub Actions / 로컬 공용)
============================================================
보내는 정보:
  0) 한눈에 보기   내 ETF 평균·미국장 방향·환율·시장 분위기 요약
  1) 내 ETF        가격 + 오늘/주간/월간 변동 + 20/60/120일 이평선 위치(▲▼)
  2) 간밤 미국 지수  나스닥100(^NDX), S&P500(^GSPC)
  3) 원/달러 환율    KRW=X
  4) 시장 분위기     VIX(^VIX) + CNN 공포·탐욕 지수
  5) 최신 뉴스       구글 뉴스 RSS(최근·최신순, 제목=링크) + Claude 요약(선택)

[준비물]  pip install requests yfinance anthropic
[Secrets] TELEGRAM_TOKEN(필수) / CHAT_ID(필수) / ANTHROPIC_API_KEY(선택)
"""

import os
import html
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import yfinance as yf

# ============================================================
# 설정
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "여기에_봇_토큰_또는_Secrets사용")
CHAT_ID = os.environ.get("CHAT_ID", "여기에_본인_chat_id_또는_Secrets사용")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

HOLDINGS = {
    "133690.KS": "TIGER 미국나스닥100",
    "360750.KS": "TIGER 미국S&P500",
}

NEWS_QUERY = "미국 증시 나스닥 S&P500 원달러 환율"

PERIODS = [("오늘", 1), ("주간", 5), ("월간", 20)]
MA_WINDOWS = [20, 60, 120]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

FG_KO = {
    "extreme fear": ("극도의 공포", "😱"),
    "fear": ("공포", "😨"),
    "neutral": ("중립", "😐"),
    "greed": ("탐욕", "😀"),
    "extreme greed": ("극도의 탐욕", "🤑"),
}

esc = html.escape

# ============================================================
# 데이터 수집·분석
# ============================================================

def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        print("텔레그램 전송 실패:", resp.text)
    return resp.ok


def get_yf_closes(symbol, period="1y"):
    hist = yf.Ticker(symbol).history(period=period)
    if hist is None or hist.empty:
        return None
    return hist["Close"]


def analyze(closes):
    if closes is None:
        return None
    closes = closes.dropna()
    closes = closes[closes > 0]
    if len(closes) < 2:
        return None
    last = float(closes.iloc[-1])
    changes = {}
    for label, n in PERIODS:
        if len(closes) > n:
            prev = float(closes.iloc[-1 - n])
            changes[label] = (last - prev) / prev * 100 if prev else None
        else:
            changes[label] = None
    mas = {w: (float(closes.iloc[-w:].mean()) if len(closes) >= w else None)
           for w in MA_WINDOWS}
    return {"last": last, "changes": changes, "mas": mas}


def safe_analyze(symbol):
    """조회/분석 중 오류가 나도 None을 돌려줘 전체가 멈추지 않게 한다."""
    try:
        return analyze(get_yf_closes(symbol))
    except Exception as e:
        print(f"{symbol} 조회 오류:", e)
        return None


def get_fear_greed():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    data = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"},
                        timeout=15).json()
    fg = data.get("fear_and_greed")
    if fg and "score" in fg:
        return float(fg["score"]), str(fg.get("rating", "")).lower()
    last = data["fear_and_greed_historical"]["data"][-1]
    return float(last["y"]), str(last.get("rating", "")).lower()


# ============================================================
# 표시 포맷
# ============================================================

def arrow_em(change):
    return "🔺" if change > 0 else ("🔻" if change < 0 else "▪️")


def headline(value_str, change):
    """가격 옆 오늘 변동.  예) 200,150원  🔺 +1.2%"""
    if change is None:
        return value_str
    return f"{value_str}  {arrow_em(change)} {change:+.1f}%"


def sub_periods(result):
    """보조줄: 주간/월간.  예) 주간 +2.8% · 월간 -1.5%"""
    parts = []
    for label, _ in PERIODS[1:]:
        v = result["changes"].get(label)
        if v is not None:
            parts.append(f"{label} {v:+.1f}%")
    return " · ".join(parts)


def trend_line(result):
    """이평선 위/아래.  예) 추세 20일▲ 60일▲ 120일▼"""
    last = result["last"]
    parts = [f"{w}일{'▲' if last >= m else '▼'}"
             for w in MA_WINDOWS
             if (m := result["mas"].get(w)) is not None]
    return ("추세 " + " ".join(parts)) if parts else ""


def vix_label(v: float) -> str:
    if v < 15:
        return "매우 안정"
    if v < 20:
        return "안정"
    if v < 30:
        return "경계"
    return "공포(변동성 큼)"


def avg_today(results):
    """오늘 변동률 평균 (요약용)."""
    vals = [r["changes"].get("오늘") for r in results if r]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def dir_word(values):
    """여러 변동률 평균으로 강세/약세/보합 한 단어."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    return "강세" if avg > 0.3 else ("약세" if avg < -0.3 else "보합")


# ============================================================
# 뉴스
# ============================================================

def _fetch_rss(query, window):
    q = f"{query} when:{window}"
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=ko&gl=KR&ceid=KR:ko"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    root = ET.fromstring(resp.content)
    out = []
    for item in root.findall(".//item"):
        title = item.findtext("title", "")
        link = item.findtext("link", "")
        pub = item.findtext("pubDate", "")
        if not title:
            continue
        try:
            dt = parsedate_to_datetime(pub) if pub else None
        except Exception:
            dt = None
        out.append((title, link, dt))
    return out


def fetch_news(query, n=5):
    items = _fetch_rss(query, "1d") or _fetch_rss(query, "7d")
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda x: x[2] or oldest, reverse=True)
    return [(t, l) for t, l, _ in items[:n]]


def summarize_news(titles):
    if not ANTHROPIC_API_KEY or not titles:
        return None
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    joined = "\n".join(f"- {t}" for t in titles)
    prompt = (
        "아래는 미국 증시·환율 관련 최신 뉴스 제목 모음입니다. "
        "미국 지수 ETF(나스닥100/S&P500)를 가진 한국 투자자가 오늘 알아두면 좋을 핵심만 "
        "3줄 이내로 각 줄 앞에 • 를 붙여 한국어로 요약하세요. "
        "투자 권유나 추측은 하지 말고 사실 위주로 적으세요.\n\n" + joined
    )
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ============================================================
# 리포트 조립
# ============================================================

def build_report() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ---- 시세 한 번만 수집 (요약·상세 공용) ----
    etfs = [(name, safe_analyze(sym)) for sym, name in HOLDINGS.items()]
    ndx = safe_analyze("^NDX")
    spx = safe_analyze("^GSPC")
    fx = safe_analyze("KRW=X")
    vix = safe_analyze("^VIX")
    try:
        fg_score, fg_rating = get_fear_greed()
    except Exception:
        fg_score, fg_rating = None, None

    L = [f"📊 <b>오늘의 시황</b>   {now}"]

    # ── 한눈에 보기 (요약) ───────────────────
    L.append("")
    L.append("🧭 <b>한눈에 보기</b>")
    etf_avg = avg_today([r for _, r in etfs])
    if etf_avg is not None:
        L.append(f"내 ETF 평균  {arrow_em(etf_avg)} {etf_avg:+.1f}%")
    bits = []
    us = dir_word([ndx["changes"].get("오늘") if ndx else None,
                   spx["changes"].get("오늘") if spx else None])
    if us:
        bits.append(f"미국장 {us}")
    if fx and fx["changes"].get("오늘") is not None:
        ft = fx["changes"]["오늘"]
        bits.append(f"환율 {arrow_em(ft)} {ft:+.1f}%")
    if bits:
        L.append(" · ".join(bits))
    mood = []
    if fg_score is not None:
        ko, em = FG_KO.get(fg_rating, (fg_rating, ""))
        mood.append(f"{em} {esc(ko)} {fg_score:.0f}")
    if vix:
        mood.append(f"VIX {vix['last']:.0f} {vix_label(vix['last'])}")
    if mood:
        L.append("분위기  " + " · ".join(mood))

    # ── 내 ETF ───────────────────────────────
    L.append("")
    L.append("💼 <b>내 ETF</b>")
    for name, r in etfs:
        L.append("")
        if r is None:
            L.append(f"<b>{esc(name)}</b>  데이터 없음")
            continue
        L.append(f"<b>{esc(name)}</b>")
        L.append(headline(f"{r['last']:,.0f}원", r["changes"].get("오늘")))
        if (sp := sub_periods(r)):
            L.append(sp)
        if (tl := trend_line(r)):
            L.append(tl)

    # ── 간밤 미국장 ──────────────────────────
    L.append("")
    L.append("🗽 <b>간밤 미국장</b>")
    for r, label in [(ndx, "나스닥100"), (spx, "S&P500")]:
        L.append("")
        if r is None:
            L.append(f"<b>{esc(label)}</b>  데이터 없음")
            continue
        L.append(f"<b>{esc(label)}</b>  " + headline(f"{r['last']:,.0f}", r["changes"].get("오늘")))
        if (sp := sub_periods(r)):
            L.append(sp)

    # ── 환율 ─────────────────────────────────
    L.append("")
    if fx is None:
        L.append("💱 <b>원/달러</b>  데이터 없음")
    else:
        L.append("💱 <b>원/달러</b>  " + headline(f"{fx['last']:,.0f}원", fx["changes"].get("오늘")))
        if (sp := sub_periods(fx)):
            L.append(sp)

    # ── 시장 분위기 ──────────────────────────
    L.append("")
    L.append("🌡️ <b>시장 분위기</b>")
    L.append(f"VIX {vix['last']:,.1f} · {vix_label(vix['last'])}" if vix else "VIX  데이터 없음")
    if fg_score is not None:
        ko, em = FG_KO.get(fg_rating, (fg_rating, ""))
        L.append(f"공포·탐욕 {fg_score:.0f}/100 {em} {esc(ko)}")
    else:
        L.append("공포·탐욕  데이터 없음")

    # ── 최신 뉴스 ────────────────────────────
    L.append("")
    L.append("📰 <b>최신 뉴스</b>")
    try:
        items = fetch_news(NEWS_QUERY)
        summary = summarize_news([t for t, _ in items])
        if summary:
            L.append(esc(summary))
        if items:
            if summary:
                L.append("")
            for title, link in items[:5]:
                L.append(f'• <a href="{esc(link)}">{esc(title)}</a>' if link else f"• {esc(title)}")
        elif not summary:
            L.append("뉴스를 불러오지 못했습니다.")
    except Exception as e:
        L.append(f"뉴스 조회 오류 ({esc(str(e))})")

    L.append("")
    L.append("<i>▲/▼ = 현재가가 이평선 위/아래 · 참고용</i>")
    return "\n".join(L)


if __name__ == "__main__":
    report = build_report()
    send_telegram(report)
    print("전송 완료\n")
    print(report)
