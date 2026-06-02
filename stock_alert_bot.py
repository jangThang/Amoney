"""
보유종목 텔레그램 알림 봇 (GitHub Actions / 로컬 공용)
------------------------------------------------------
- pykrx로 한국 주식(KRX) 시세를 조회합니다.
- 종목별 종가/등락률을 정리해 텔레그램으로 보냅니다.
- 전일 대비 변동률이 임계치(ALERT_THRESHOLD)를 넘으면 별도 경고를 붙입니다.

[준비물]  pip install pykrx requests

[토큰 설정]
- GitHub Actions에서 실행: 저장소 Secrets에 TELEGRAM_TOKEN, CHAT_ID 등록 (코드 수정 불필요)
- 로컬에서 테스트: 아래 기본값 자리에 직접 넣거나, 터미널에서 환경변수로 지정
"""

import os
import requests
from datetime import datetime, timedelta
from pykrx import stock

# ============================================================
# 설정
# ============================================================

# 환경변수에서 먼저 읽고, 없으면 따옴표 안의 기본값을 사용 (로컬 테스트용)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "여기에_봇_토큰_또는_Secrets사용")
CHAT_ID = os.environ.get("CHAT_ID", "여기에_본인_chat_id_또는_Secrets사용")

# 보유 종목 { "종목코드": "종목명" }  (코드는 6자리)
HOLDINGS = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035720": "카카오",
}

# 전일 대비 ±몇 % 이상 움직이면 경고를 붙일지
ALERT_THRESHOLD = 3.0

# ============================================================
# 기능 함수
# ============================================================

def send_telegram(message: str) -> bool:
    """텔레그램 봇으로 메시지를 전송한다."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    resp = requests.post(url, data=payload, timeout=10)
    if not resp.ok:
        print("텔레그램 전송 실패:", resp.text)
    return resp.ok


def get_latest_quote(ticker: str):
    """최근 영업일 종가와 등락률을 반환한다. 반환: (종가, 등락률) 또는 (None, None)"""
    today = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    df = stock.get_market_ohlcv(start, today, ticker)
    if df is None or df.empty:
        return None, None
    last = df.iloc[-1]
    return int(last["종가"]), float(last["등락률"])


def build_report() -> str:
    """보유종목 전체를 조회해 텔레그램용 메시지 문자열을 만든다."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 <b>보유종목 시황</b>  ({now})", ""]
    alerts = []

    for ticker, name in HOLDINGS.items():
        try:
            close, change = get_latest_quote(ticker)
            if close is None:
                lines.append(f"• {name}({ticker}): 데이터 없음")
                continue

            arrow = "🔺" if change > 0 else ("🔻" if change < 0 else "➖")
            lines.append(f"• {name}: {close:,}원  {arrow}{change:+.2f}%")

            if abs(change) >= ALERT_THRESHOLD:
                alerts.append(f"⚠️ {name} {change:+.2f}% (임계치 {ALERT_THRESHOLD}% 초과)")
        except Exception as e:
            lines.append(f"• {name}({ticker}): 조회 오류 ({e})")

    if alerts:
        lines.append("")
        lines.append("<b>※ 변동 알림</b>")
        lines.extend(alerts)

    return "\n".join(lines)


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":
    report = build_report()
    send_telegram(report)
    print("전송 완료\n")
    print(report)
