"""
미국지수 ETF 보유자용 텔레그램 알림 봇 (GitHub Actions / 로컬 공용)
============================================================
보내는 정보 4가지:
  1) 내 ETF 가격·등락률            (pykrx / KRX)
  2) 간밤 미국 지수 변동           (yfinance: ^NDX 나스닥100, ^GSPC S&P500)
  3) 원/달러 환율                  (yfinance: KRW=X)  ← 환노출형이라 수익률에 직접 영향
  4) 미국 증시·환율 뉴스 AI 요약   (구글 뉴스 RSS + Claude Haiku)

[준비물]
  pip install pykrx requests yfinance anthropic

[필요한 키 / Secrets]
  TELEGRAM_TOKEN      (필수) 텔레그램 봇 토큰
  CHAT_ID            (필수) 내 채팅 ID
  ANTHROPIC_API_KEY  (선택) 있으면 뉴스 AI 요약, 없으면 뉴스 제목만 표시
                            console.anthropic.com 에서 발급
"""

import os
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

# 보유 ETF { "종목코드": "표시이름" }
HOLDINGS = {
    "133690": "TIGER 미국나스닥100",
    "360750": "TIGER 미국S&P500",
}

# 뉴스 검색어 (구글 뉴스 RSS)
NEWS_QUERY = "미국 증시 나스닥 S&P500 원달러 환율"

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


# ============================================================
# 1) 내 ETF 가격 (pykrx)
# ============================================================

def get_etf_change(ticker: str):
    """최근 종가와 직전 대비 등락률을 계산한다. 반환: (종가, 등락률) 또는 (None, None)"""
    today = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
    df = stock.get_etf_ohlcv_by_date(start, today, ticker)
    if df is None or df.empty:
        return None, None
    closes = df["종가"]
    closes = closes[closes > 0]  # 거래 없는 날(0) 제거
    if len(closes) < 2:
        return None, None
    last, prev = closes.iloc[-1], closes.iloc[-2]
    change = (last - prev) / prev * 100
    return int(last), float(change)


# ============================================================
# 2) & 3) 미국 지수 + 환율 (yfinance)
# ============================================================

def get_yf_change(symbol: str):
    """야후 파이낸스에서 최근 종가와 직전 대비 등락률을 계산한다."""
    hist = yf.Ticker(symbol).history(period="7d")
    if hist is None or hist.empty:
        return None, None
    closes = hist["Close"].dropna()
    if len(closes) < 2:
        return None, None
    last, prev = closes.iloc[-1], closes.iloc[-2]
    change = (last - prev) / prev * 100
    return float(last), float(change)


# ============================================================
# 4) 뉴스 (구글 뉴스 RSS) + AI 요약 (Claude Haiku)
# ============================================================

def fetch_news(query: str, n: int = 6):
    """구글 뉴스 RSS에서 최신 기사 제목을 가져온다. (API 키 불필요)"""
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    root = ET.fromstring(resp.content)
    titles = [item.findtext("title", "") for item in root.findall(".//item")[:n]]
    return [t for t in titles if t]


def summarize_news(headlines):
    """ANTHROPIC_API_KEY가 있으면 Claude로 3줄 요약, 없으면 None."""
    if not ANTHROPIC_API_KEY or not headlines:
        return None
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    joined = "\n".join(f"- {h}" for h in headlines)
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
            close, change = get_etf_change(ticker)
            if close is None:
                lines.append(f"• {name}: 데이터 없음")
            else:
                lines.append(f"• {name}: {close:,}원 {arrow(change)}{change:+.2f}%")
        except Exception as e:
            lines.append(f"• {name}: 조회 오류 ({e})")

    # 2) 간밤 미국 지수
    lines.append("")
    lines.append("<b>🌙 간밤 미국장</b>")
    for symbol, label in [("^NDX", "나스닥100"), ("^GSPC", "S&P500")]:
        try:
            val, change = get_yf_change(symbol)
            if val is None:
                lines.append(f"• {label}: 데이터 없음")
            else:
                lines.append(f"• {label}: {val:,.2f} {arrow(change)}{change:+.2f}%")
        except Exception as e:
            lines.append(f"• {label}: 조회 오류 ({e})")

    # 3) 원/달러 환율
    lines.append("")
    try:
        fx, fx_change = get_yf_change("KRW=X")
        if fx is None:
            lines.append("💱 원/달러: 데이터 없음")
        else:
            lines.append(f"💱 <b>원/달러</b>: {fx:,.2f}원 {arrow(fx_change)}{fx_change:+.2f}%")
    except Exception as e:
        lines.append(f"💱 원/달러: 조회 오류 ({e})")

    # 4) 뉴스
    lines.append("")
    lines.append("<b>📰 뉴스</b>")
    try:
        headlines = fetch_news(NEWS_QUERY)
        summary = summarize_news(headlines)
        if summary:
            lines.append(summary)
        elif headlines:
            # API 키가 없을 때: 제목 상위 4개만
            for h in headlines[:4]:
                lines.append(f"• {h}")
        else:
            lines.append("• 뉴스를 불러오지 못했습니다.")
    except Exception as e:
        lines.append(f"• 뉴스 조회 오류 ({e})")

    return "\n".join(lines)


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":
    report = build_report()
    send_telegram(report)
    print("전송 완료\n")
    print(report)
