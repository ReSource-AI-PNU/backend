"""v3 시험 공통 설정.

시연 시드는 허가 확인 상태 세 가지를 보인다(새길자원 대조 완료 · 남해열분해 검토 대기 · 미소환경 원본 확인 필요).
확정까지 가는 기존 시험은 허가 확인이 아닌 다른 판단을 보므로, 두 배출 기업 담당자가 세 업체의 허가증 원문을 모두
대조해 둔 시드(V3_DEMO_PERMITS=verified)로 돈다. 허가 확인 상태는 test_permit_verification.py와
test_demo_seed.py가 기본 시드(mixed)로 따로 본다.
"""

import os

os.environ.setdefault("V3_DEMO_PERMITS", "verified")
