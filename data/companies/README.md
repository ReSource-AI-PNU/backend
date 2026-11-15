# 가상 기업 시드

원본: ReSource-AI-PNU/frontend, `feat/walkthrough`의 `b783fb1`, `public/data/companies/*.json`.
8개 단지 100곳이며 녹산은 30곳입니다. 회사명·허가·물량은 실제 검증 자료가 아닙니다.

`python -m scripts.seed_platform --demo`가 `CompanyProfile`로 검증하고 기존 행을 보존하며 적재합니다.
수지 표기가 불명확한 PE 일반 표기 등은 임의로 LDPE로 바꾸지 않습니다.
녹산 킥 장면에 필요한 300/420/510kg, 수지·출처·업체 조건은 스크립트의 명시적 가상 입력입니다.

운영 모드에서는 가상 기업을 매칭에 넣지 않습니다. 실기업 허가 확인은 원문 확인 후 별도 운영 절차가 필요합니다.
