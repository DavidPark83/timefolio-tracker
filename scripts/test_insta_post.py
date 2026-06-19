import os, requests, sys, time
from dotenv import load_dotenv

load_dotenv()

IG_USER = os.getenv("IG_USER_ID")
TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

if not (IG_USER and TOKEN):
    sys.exit("❌ .env 누락")

TEST_IMG = "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=1080&h=1080&fit=crop"

print(f"IG_USER_ID: {IG_USER}")
print(f"TOKEN: {TOKEN[:15]}...{TOKEN[-8:]}")

# 1) 컨테이너 생성
print("\n[1/3] 컨테이너 생성 중...")
r1 = requests.post(
    f"https://graph.facebook.com/v21.0/{IG_USER}/media",
    data={
        "image_url": TEST_IMG,
        "caption": "🧪 Active ETF Tracker API 검증 테스트 #test",
        "access_token": TOKEN,
    },
)
print("응답:", r1.json())
r1.raise_for_status()
creation_id = r1.json()["id"]

# 2) 컨테이너 상태 폴링 (FINISHED될 때까지)
print(f"\n[2/3] 미디어 처리 대기 중 (creation_id={creation_id})...")
max_attempts = 20  # 최대 20회 × 3초 = 60초
for attempt in range(1, max_attempts + 1):
    time.sleep(3)
    rs = requests.get(
        f"https://graph.facebook.com/v21.0/{creation_id}",
        params={"fields": "status_code,status", "access_token": TOKEN},
    )
    status = rs.json().get("status_code", "UNKNOWN")
    print(f"  시도 {attempt}/{max_attempts}: status_code={status}")
    if status == "FINISHED":
        print("  ✅ 처리 완료, 게시 가능")
        break
    elif status == "ERROR":
        sys.exit(f"❌ 미디어 처리 실패: {rs.json()}")
    elif status == "EXPIRED":
        sys.exit("❌ 컨테이너 만료됨 (24시간 경과)")
else:
    sys.exit("❌ 60초 내 처리 완료 안 됨. 이미지 크기/형식 확인.")

# 3) 게시
print("\n[3/3] 게시 중...")
r2 = requests.post(
    f"https://graph.facebook.com/v21.0/{IG_USER}/media_publish",
    data={"creation_id": creation_id, "access_token": TOKEN},
)
print("응답:", r2.json())
r2.raise_for_status()

print(f"\n🎉 성공! media_id: {r2.json()['id']}")
print("→ 인스타 앱에서 active.etf.tracker 피드 확인하세요.")