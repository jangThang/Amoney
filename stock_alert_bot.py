"""
미국지수 ETF 보유자용 텔레그램 알림 봇 (GitHub Actions / 로컬 공용)
============================================================
보내는 정보:
  1) 내 ETF        가격 + 1일/1주/1달 변동률 + 20/60/120일 이평선 위치  (pykrx / KRX)
  2) 간밤 미국 지수  나스닥100(^NDX), S&P500(^GSPC) + 1일/1주/1달 변동    (yfinance)
  3) 원/달러 환율    KRW=X + 1일/1주/1달 변동                            (yfinance)
  4) 시장 분위기     VIX(^VIX) + CNN 공포·탐욕 지수                       (yfinance / CNN)
  5) 뉴스           구글 뉴스 RSS (제목=기사 링크) + Claude Haiku 요약    (선택)

[준비물]  pip install pykrx requests yfinance anthropic

[Secrets]
  TELEGRAM_TOKEN      (필수)
  CHAT_ID            (필수)
  ANTHROPIC_API_KEY  (선택)  없으면 뉴스 요약은 생략하고 링크만 표시
"""

import os
import html
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime, timedelta

from pykrx import stock
import yfinance as yf

# ============================================================
# 설정
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "여기에_봇_토큰_또는_Secrets사용")
CHAT_ID = os.environ.get("CHAT_ID", "여기에_본인_chat_id_또는_Secrets사용")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # 없으면 None

HOLDINGS = {
    "133690": "TIGER 미국나스닥100",
    "360750": "TIGER 미국S&P500",
}

NEWS_QUERY = "미국 증시 나스닥 S&P500 원달러 환율"

PERIODS = [("1일", 1), ("1주", 5), ("1달", 20)]   # 영업일 기준
MA_WINDOWS = [20, 60, 120]                         # 이동평균선

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 공포·탐욕 지수 등급 → (한글, 이모지)
FG_KO = {
    "extreme fear": ("극도의 공포", "😱"),
    "fear": ("공포", "😨"),
    "neutral": ("중립", "😐"),
    "greed": ("탐욕", "😀"),
    "extreme greed": ("극도의 탐욕", "🤑"),
}

esc = html.escape  # 텔레그램 HTML 모드용 텍스트 escape (& < > 처리)

# ============================================================
# 공통 유틸
# ============================================================

def arrow(change: float) -> str:
    return "🔺" if change > 0 else ("🔻" if change < 0 else "➖")


def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        print("텔레그램 전송 실패:", resp.text)
    return resp.ok


def analyze(closes):
    """종가 시계열(오래된→최신)을 받아 {last, changes, mas} 반환."""
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


def fmt_changes(result) -> str:
    parts = []
    for label, _ in PERIODS:
        v = result["changes"].get(label)
        parts.append(f"{label} -" if v is None else f"{label} {arrow(v)}{v:+.2f}%")
    return " · ".join(parts)


def fmt_mas(result, dec=0) -> str:
    last = result["last"]
    parts = []
    for w in MA_WINDOWS:
        m = result["mas"].get(w)
        if m is None:
            parts.append(f"{w}일 -")
        else:
            parts.append(f"{w}일 {m:,.{dec}f}{'▲' if last >= m else '▼'}")
    return " · ".join(parts)


# ============================================================
# 데이터 수집
# ============================================================

def get_etf_closes(ticker):
    today = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=250)).strftime("%Y%m%d")
    df = stock.get_etf_ohlcv_by_date(start, today, ticker)
    if df is None or df.empty:
        return None
    return df["종가"]


def get_yf_closes(symbol):
    hist = yf.Ticker(symbol).history(period="6mo")
    if hist is None or hist.empty:
        return None
    return hist["Close"]


def vix_label(v: float) -> str:
    if v < 15:
        return "매우 안정"
    if v < 20:
        return "안정"
    if v < 30:
        return "경계"
    return "공포(변동성 큼)"


def get_fear_greed():
    """CNN 공포·탐욕 지수 현재값과 등급을 반환. (score, rating)"""
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    data = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"},
                        timeout=15).json()
    fg = data.get("fear_and_greed")
    if fg and "score" in fg:
        return float(fg["score"]), str(fg.get("rating", "")).lower()
    hist = data["fear_and_greed_historical"]["data"]  # 폴백: 마지막 값
    last = hist[-1]
    return float(last["y"]), str(last.get("rating", "")).lower()


# ============================================================
# 뉴스 (구글 뉴스 RSS) + AI 요약
# ============================================================

def fetch_news(query, n=6):
    """(제목, 링크) 튜플 리스트를 반환."""
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    root = ET.fromstring(resp.content)
    items = []
    for item in root.findall(".//item")[:n]:
        title = item.findtext("title", "")
        link = item.findtext("link", "")
        if title:
            items.append((title, link))
    return items


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
    lines = [f"📊 <b>오늘의 시황</b>  ({now})", ""]

    # 1) 내 ETF
    lines.append("<b>📌 내 ETF</b>")
    for ticker, name in HOLDINGS.items():
        try:
            r = analyze(get_etf_closes(ticker))
            if r is None:
                lines.append(f"• {esc(name)}: 데이터 없음")
                continue
            lines.append(f"• <b>{esc(name)}</b>: {r['last']:,.0f}원")
            lines.append(f"  {fmt_changes(r)}")
            lines.append(f"  이평 {fmt_mas(r, dec=0)}")
        except Exception as e:
            lines.append(f"• {esc(name)}: 조회 오류 ({esc(str(e))})")

    # 2) 간밤 미국 지수
    lines.append("")
    lines.append("<b>🌙 간밤 미국장</b>")
    for symbol, label in [("^NDX", "나스닥100"), ("^GSPC", "S&P500")]:
        try:
            r = analyze(get_yf_closes(symbol))
            if r is None:
                lines.append(f"• {esc(label)}: 데이터 없음")
                continue
            lines.append(f"• <b>{esc(label)}</b>: {r['last']:,.2f}")
            lines.append(f"  {fmt_changes(r)}")
        except Exception as e:
            lines.append(f"• {esc(label)}: 조회 오류 ({esc(str(e))})")

    # 3) 원/달러 환율
    lines.append("")
    try:
        r = analyze(get_yf_closes("KRW=X"))
        if r is None:
            lines.append("💱 원/달러: 데이터 없음")
        else:
            lines.append(f"💱 <b>원/달러</b>: {r['last']:,.2f}원")
            lines.append(f"  {fmt_changes(r)}")
    except Exception as e:
        lines.append(f"💱 원/달러: 조회 오류 ({esc(str(e))})")

    # 4) 시장 분위기 (VIX + 공포·탐욕)
    lines.append("")
    lines.append("<b>🌡️ 시장 분위기</b>")
    try:
        r = analyze(get_yf_closes("^VIX"))
        if r is None:
            lines.append("• VIX(변동성): 데이터 없음")
        else:
            ch = r["changes"].get("1일")
            chtxt = f"{arrow(ch)}{ch:+.2f}%" if ch is not None else ""
            lines.append(f"• VIX(변동성): {r['last']:,.2f} {chtxt} — {vix_label(r['last'])}")
    except Exception as e:
        lines.append(f"• VIX: 조회 오류 ({esc(str(e))})")
    try:
        score, rating = get_fear_greed()
        ko, emoji = FG_KO.get(rating, (rating, ""))
        lines.append(f"• 공포·탐욕 지수: {score:.0f}/100 {emoji} {esc(ko)}")
    except Exception as e:
        lines.append(f"• 공포·탐욕 지수: 조회 오류 ({esc(str(e))})")

    # 5) 뉴스 (제목 = 기사 링크)
    lines.append("")
    lines.append("<b>📰 뉴스</b>")
    try:
        items = fetch_news(NEWS_QUERY)
        summary = summarize_news([t for t, _ in items])
        if summary:
            lines.append(esc(summary))
            lines.append("")
            lines.append("<b>관련 기사</b>")
        if items:
            for title, link in items[:5]:
                if link:
                    lines.append(f'• <a href="{esc(link)}">{esc(title)}</a>')
                else:
                    lines.append(f"• {esc(title)}")
        elif not summary:
            lines.append("• 뉴스를 불러오지 못했습니다.")
    except Exception as e:
        lines.append(f"• 뉴스 조회 오류 ({esc(str(e))})")

    lines.append("")
    lines.append("<i>※ ▲=현재가가 해당 이평선 위 / ▼=아래. 참고용 정보입니다.</i>")
    return "\n".join(lines)


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":
    report = build_report()
    send_telegram(report)
    print("전송 완료\n")
    print(report)
