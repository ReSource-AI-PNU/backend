"""운영자 명령(V3_AUTH=bff): 관리자 조직에 사용자 넣기, 시연 조직 되돌리기, 사용자 찾기·지우기.

백엔드 컨테이너 안에서 서버와 같은 설정(DATABASE_URL 등)으로 돈다. HTTP를 거치지 않으므로 관리자
조직의 구성원이 아직 없을 때(처음 배포)도 쓸 수 있다.

    python -m scripts.v3.accounts_admin reset-demo            # 시연 조직만 되돌리고 시연 진단을 돌려 둔다
    python -m scripts.v3.accounts_admin grant-admin <이메일>  # 가입한 사용자를 org_admin의 owner로 넣는다
    python -m scripts.v3.accounts_admin list-users [--like <찾기 글>]  # 한 줄에 사용자 하나(JSON)
    python -m scripts.v3.accounts_admin delete-user <이메일> [--force-org]  # 탈퇴와 같은 규칙으로 지운다

grant-admin 전에 그 사용자가 화면에서 가입해 있어야 하고, 시연 데이터(org_admin)가 올라가 있어야 한다.
list-users의 --like는 이메일이나 이름에서 찾는다(대소문자를 가리지 않는다. *와 %는 아무 글자열이고,
와일드카드가 없으면 포함 검색이다). 비밀번호 값은 내보내지 않는다.
delete-user는 탈퇴(DELETE /accounts/me)와 같이 지운다(계약 9장 "탈퇴"). 혼자 owner인 조직에 다른 구성원이
있으면 지우지 않고 멈춘다. --force-org를 주면 그런 조직도 구성원의 소속과 함께 지운다.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from typing import Any

from app.v3 import accounts, service
from app.v3.demo.seed import ADMIN


def main(argv: list[str] | None = None, app: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Re:Source AI v3 운영자 명령(bff 모드)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("reset-demo", help="시연 조직만 처음 상태로 되돌리고 시연 진단을 돌려 둔다")
    grant = commands.add_parser("grant-admin", help="가입한 사용자를 관리자 조직(owner)에 넣는다")
    grant.add_argument("email")
    listing = commands.add_parser(
        "list-users", help="사용자를 찾는다(비밀번호 값은 내보내지 않는다)"
    )
    listing.add_argument("--like", default=None, help="이메일이나 이름의 찾기 글(*, % 와일드카드)")
    remove = commands.add_parser("delete-user", help="사용자를 탈퇴와 같은 규칙으로 지운다")
    remove.add_argument("email")
    remove.add_argument(
        "--force-org",
        action="store_true",
        help="혼자 owner인 조직에 다른 구성원이 있어도 그 조직을 지운다",
    )
    args = parser.parse_args(argv)
    if app is None:
        from app.v3.api import build_app

        app = build_app()
    if app.auth_mode != "bff":
        # 다른 모드의 되돌리기는 저장소 전체를 비운다. 이 명령은 실제 계정이 있는 bff 모드에서만 쓴다
        print("오류: V3_AUTH=bff에서만 쓰는 명령입니다.", file=sys.stderr)
        return 2
    if args.command == "reset-demo":
        # 운영자 명령이므로 관리자 확인을 건너뛴다(관리자 조직이 아직 없을 때도 돈다)
        trusted = copy.copy(app)
        trusted.demo_open = True
        summary = service.reset_demo(trusted)
        print(
            json.dumps(
                {
                    "demoSessionId": summary.get("demoSessionId"),
                    "demoSessionStatus": summary.get("demoSessionStatus"),
                    "orgIds": summary.get("orgIds"),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "list-users":
        for user in accounts.list_users(app, args.like):
            print(json.dumps(user, ensure_ascii=False))
        return 0
    if args.command == "delete-user":
        try:
            summary = accounts.delete_user(app, args.email, force_orgs=args.force_org)
        except ValueError as error:
            print(f"오류: {error}", file=sys.stderr)
            return 2
        except service.ServiceError as error:
            print(f"오류: {error.code}: {error.message}", file=sys.stderr)
            return 2
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    try:
        membership = accounts.grant_membership(app, args.email, ADMIN, "owner")
    except ValueError as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2
    print(json.dumps(membership, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
