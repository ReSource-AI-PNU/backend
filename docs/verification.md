# 조건 변경 재탐색·조건 비교 검증

2026-11-22. 브랜치 `work/adaptive`. 백엔드 `/Users/jeondonghun/rs-work/backend-adaptive`, 프런트 `/Users/jeondonghun/rs-work/frontend-adaptive`.

## 구현한 동작

- 업체가 프로필 또는 확인한 매입 기준표를 바꾸면, 해당 회사의 최신 진단에서 실제 합의 조건에 영향을 받는지 대조한다. 영향받는 승인 대기·확정 진단에 새 대안을 연결하고 백그라운드로 실행한다.
- 새 진단에 이전 결과를 보관해 월 손익·배정·운반비를 비교한다. 새 대안은 양쪽 승인을 다시 받아야 한다. 이전 확정 거래의 문서·승인·검수는 보존한다.
- 결과 화면에서 폐기물별 월 발생량과 전체 폐기물에 적용할 월 최대 수거 횟수를 비교한다. 최대 횟수는 업체의 현재 수거 조건으로 후보를 제한하며, 미확인 업체는 제외한다.
- 미리보기는 메모리 저장소에서 규칙 정책·기존 판단 도구로 계산하고 원본을 쓰지 않는다. 실제 재협상은 앱에 설정된 모델을 쓴다. 미리보기 결과와 최종 협상 결과는 다를 수 있다고 화면에 표시한다.
- 비교 token은 진단·사업장·폐기물·후보 업체·기준일·입력에 묶인다. 적용 전 변경이 있으면 다시 비교한다. 같은 token 재전송은 이미 만든 진단을 반환한다.
- HTTP 요청 뒤 저장된 큐를 실행한다. 서버 재시작 후 남은 작업은 다음 요청 때 재개한다. 실행 자리로 중복을 막고 자동 재시도는 최대 세 번이다. 추가 질문에 임의 값을 넣지 않고 기존 담당자 답만 재사용한다.

## 실행 검증

| 확인 방법 | 기대 결과 | 실제 결과 | 판정 |
|---|---|---|---|
| `pytest tests/v3/test_adaptive.py -q` | 미리보기 격리, stale token·타 조직·비정상 수치 거부, 동일 적용·실행 중복 방지, 프로필·기준표 변경, 과거 문서 보존 | 13개 통과 | 통과 |
| 전체 백엔드 `tests/**/test_*.py` (파일 단위 4개 프로세스, 분리된 pytest cache) | 기존 동작 회귀 없음 | 1,959개 통과, 5개 건너뜀, 40개 하위 테스트 통과 | 통과 |
| `pytest tests/v3/test_vendor_service.py tests/v3/test_api_wiring.py tests/v3/test_fix2_contract_docs.py -q` | 별도 업체 AI 수신, API 경로·오류 문서 일치 | 36개 통과 | 통과 |
| 프런트 `node --test tests/*.test.ts` | 기존 계산·표시·권한 정책 회귀 없음 | 272개 통과, 2개 건너뜀 | 통과 |
| 프런트 `next typegen`, `tsc --noEmit`, `npm run lint`, `npm run build` | 타입·린트·프로덕션 빌드 성공 | 성공 | 통과 |
| Playwright 전체 | 기존 찾기·양쪽 승인·조직 경계·체험하기와 새 조건 비교 흐름 | 7개 통과, bff 계정 가입 1개 건너뜀 | 통과 |
| 최신 백엔드 재시작 후 `e2e/adaptive.spec.ts` | 비교는 원본 600kg 보존 → 800kg 적용 → 새 승인 대기 → 업체 최소 물량 변경 → 자동 새 대안 알림 | 1개 통과, 390px 가로 넘침 없음, 화면 캡처 직접 확인 | 통과 |

## 확인하지 않은 범위

운영 배포·실제 Foundry/Ollama 호출·운영 DB 마이그레이션 검증은 수행하지 않았다. 새 테이블이나 의존성은 추가하지 않았고 기존 session.work JSON에 상태를 보관한다. 브라우저·서비스 통합 테스트는 가상 시연 자료와 로컬 규칙 정책이다. 실제 계약·외부 발송은 수행하지 않는다.

## 실행

백엔드: `python -m app.v3.devserver --port 8097`.
프런트: 기존 개발 로그인 설정으로 `NEXT_PUBLIC_API_BASE=/api/v3`, `V3_BACKEND_URL=http://localhost:8097/api/v3`, `V3_AUTH=header`를 지정하고 `npm run dev -- --port 3097`. header 모드는 로컬 검증 전용이다.

기능 테스트: `E2E_BASE_URL=http://localhost:3097 E2E_API=http://localhost:8097/api/v3 npx playwright test e2e/adaptive.spec.ts`.
