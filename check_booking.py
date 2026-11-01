"""콘래드 서울 제스트 네이버 예약 빈자리 체크.

사용법:
    python check_booking.py              # 한 번만 체크
    python check_booking.py --loop 5     # 5분마다 계속 체크
    python check_booking.py --date 2026-12-19 --people 10
    python check_booking.py --kakao-login <REST_API_키> <인가코드>   # 카톡 알림 연결 (최초 1회)
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BUSINESS_ID = "93652"  # 콘래드 서울 제스트 (place 33879318)
BUSINESS_TYPE_ID = 6
GRAPHQL_URL = "https://m.booking.naver.com/graphql"
BOOKING_URL = f"https://m.booking.naver.com/booking/{BUSINESS_TYPE_ID}/bizes/{BUSINESS_ID}"
STATE_FILE = Path(__file__).with_name("seen_items.json")
KAKAO_FILE = Path(__file__).with_name("kakao_token.json")
KAKAO_REDIRECT_URI = "https://localhost"


def gql(operation, query, variables):
    req = urllib.request.Request(
        f"{GRAPHQL_URL}?opName={operation}",
        data=json.dumps({"operationName": operation, "variables": variables, "query": query}).encode(),
        headers={
            "content-type": "application/json",
            "referer": BOOKING_URL,
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]


def fetch_biz_items():
    query = "query bizItems($input: BizItemsParams){ bizItems(input:$input){ bizItemId name minBookingCount maxBookingCount } }"
    return gql("bizItems", query, {"input": {"businessId": BUSINESS_ID, "lang": "ko"}})["bizItems"]


def fetch_slots(biz_item_id, date):
    query = (
        "query hourlySchedule($scheduleParams: ScheduleParams){ schedule(input:$scheduleParams){ bizItemSchedule{ hourly{ "
        "unitStartTime unitStock unitBookingCount isUnitSaleDay isUnitBusinessDay minBookingCount maxBookingCount } } } }"
    )
    params = {
        "businessTypeId": BUSINESS_TYPE_ID,
        "businessId": BUSINESS_ID,
        "bizItemId": biz_item_id,
        "startDateTime": f"{date}T00:00:00",
        "endDateTime": f"{date}T23:59:59",
        "fixedTime": True,
        "includesHolidaySchedules": True,
    }
    schedule = gql("hourlySchedule", query, {"scheduleParams": params})["schedule"]
    return (schedule.get("bizItemSchedule") or {}).get("hourly") or []


def bookable(slot, people):
    remaining = (slot["unitStock"] or 0) - (slot["unitBookingCount"] or 0)
    return (
        slot["isUnitSaleDay"]
        and slot["isUnitBusinessDay"]
        and remaining >= people
        and (slot["minBookingCount"] or 1) <= people <= (slot["maxBookingCount"] or people)
    )


def kakao_post(url, fields, token=None):
    headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def kakao_login(rest_key, code):
    """인가 코드를 토큰으로 바꿔 kakao_token.json에 저장."""
    tokens = kakao_post("https://kauth.kakao.com/oauth/token", {
        "grant_type": "authorization_code",
        "client_id": rest_key,
        "redirect_uri": KAKAO_REDIRECT_URI,
        "code": code,
    })
    tokens["rest_key"] = rest_key
    KAKAO_FILE.write_text(json.dumps(tokens, indent=2), "utf-8")
    print("카카오 토큰 저장 완료. 테스트 메시지를 보냅니다.")
    send_kakao("콘래드 예약 체크 알림이 연결됐어요.")


def refresh_kakao(conf):
    tokens = kakao_post("https://kauth.kakao.com/oauth/token", {
        "grant_type": "refresh_token",
        "client_id": conf["rest_key"],
        "refresh_token": conf["refresh_token"],
    })
    conf.update(tokens)  # refresh_token은 만료가 가까울 때만 새로 내려옴
    if KAKAO_FILE.exists():
        KAKAO_FILE.write_text(json.dumps(conf, indent=2), "utf-8")


def load_kakao_conf():
    # GitHub Actions에서는 Secrets로 넘어온 환경변수 사용
    if os.environ.get("KAKAO_REST_KEY") and os.environ.get("KAKAO_REFRESH_TOKEN"):
        return {
            "rest_key": os.environ["KAKAO_REST_KEY"],
            "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"],
            "access_token": "",
        }
    if KAKAO_FILE.exists():
        return json.loads(KAKAO_FILE.read_text("utf-8"))
    return None


def send_kakao(text):
    """카카오톡 '나에게 보내기'. 설정이 없으면 건너뜀."""
    conf = load_kakao_conf()
    if not conf:
        return
    template = json.dumps({
        "object_type": "text",
        "text": text[:200],
        "link": {"web_url": BOOKING_URL, "mobile_web_url": BOOKING_URL},
        "button_title": "예약하러 가기",
    }, ensure_ascii=False)
    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    if not conf["access_token"]:
        refresh_kakao(conf)
    try:
        kakao_post(url, {"template_object": template}, conf["access_token"])
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        refresh_kakao(conf)  # access_token은 몇 시간마다 만료
        kakao_post(url, {"template_object": template}, conf["access_token"])


def notify(title, message):
    print(f"\a[알림] {title}: {message}")
    try:
        send_kakao(f"{title}\n{message}")
    except Exception as e:
        print(f"  카카오톡 전송 실패: {e}")
    if sys.platform != "win32":
        return
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(10000, '{title.replace("'", "''")}', '{message.replace("'", "''")}', 'Info')
Start-Sleep -Seconds 10
$n.Dispose()
"""
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script])


def check(date, people, exclude=()):
    now = datetime.now().strftime("%H:%M:%S")
    items = fetch_biz_items()

    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else None
    if isinstance(state, list):  # 예전 형식 (상품 ID 목록)
        state = {"items": state, "slots": []}

    # 새 예약 상품(예: "12월 예약")이 열리면 알림
    if state is not None:
        for item in items:
            if item["bizItemId"] not in state["items"]:
                notify("새 예약 상품 오픈", item["name"])

    found = {}
    for item in items:
        if any(word in item["name"] for word in exclude):
            continue
        for slot in fetch_slots(item["bizItemId"], date):
            if bookable(slot, people):
                key = f"{item['bizItemId']} {slot['unitStartTime']}"
                found[key] = f"{item['name']} {slot['unitStartTime'][11:16]} (잔여 {slot['unitStock'] - slot['unitBookingCount']})"

    STATE_FILE.write_text(json.dumps({"items": sorted(i["bizItemId"] for i in items), "slots": sorted(found)}), "utf-8")

    if found:
        # 지난번에 없던 시간대가 생겼을 때만 알림 (같은 알림 반복 방지)
        if state is None or set(found) - set(state["slots"]):
            notify(f"{date} {people}명 예약 가능!", " / ".join(found.values()))
        else:
            print(f"[{now}] 예약 가능 (이미 알림 보냄): {' / '.join(found.values())}")
        print(f"  {BOOKING_URL}")
    else:
        names = ", ".join(i["name"] for i in items)
        print(f"[{now}] {date} {people}명 자리 없음 (현재 상품: {names})")
    return bool(found)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-12-19")
    parser.add_argument("--people", type=int, default=10)
    parser.add_argument("--exclude", nargs="*", default=["조식"], help="이름에 이 단어가 들어간 상품은 제외")
    parser.add_argument("--loop", type=float, metavar="MINUTES", help="N분마다 반복 체크")
    parser.add_argument("--kakao-login", nargs=2, metavar=("REST_KEY", "CODE"), help="카카오 토큰 최초 발급")
    args = parser.parse_args()

    if args.kakao_login:
        kakao_login(*args.kakao_login)
        return

    while True:
        try:
            check(args.date, args.people, args.exclude)
        except Exception as e:  # 네트워크 오류 등은 다음 주기에 재시도
            print(f"[{datetime.now():%H:%M:%S}] 오류: {e}")
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
