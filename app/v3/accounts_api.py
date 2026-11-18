"""계정·조직·초대·체험하기 HTTP 경로(V3_AUTH=bff 전용). 계약 9장이다.

다른 모드(header, token)에서는 모든 경로가 404 NOT_FOUND다. 모든 요청에 Authorization: Bearer
<V3_BFF_KEY>가 있어야 한다. 가입과 로그인, 시연 조직 목록은 사용자 없이, 나머지는 X-User-Id로 부른다.
판단은 app/v3/accounts.py가 하고 여기서는 요청을 읽어 넘긴다.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.v3 import accounts, httpio, service
from app.v3.api import bff_key_only, call, guarded, json_object

router = APIRouter(prefix="/api/v3", tags=["v3-accounts"])


def _bff(request: Request) -> httpio.BffIdentity:
    found = bff_key_only(request)
    if found is None:
        raise service.ServiceError("NOT_FOUND", "그런 주소가 없습니다.", 404)
    return found


def _user_id(request: Request) -> str:
    who = _bff(request)
    if not who.user_id:
        raise service.ServiceError("USER_REQUIRED", "X-User-Id로 사용자를 밝혀야 합니다.", 401)
    return who.user_id


def created(body: dict) -> JSONResponse:
    return JSONResponse(body, status_code=201)


def no_content() -> Response:
    return Response(status_code=204)


@router.post("/accounts/signup")
@guarded
async def signup(request: Request):
    _bff(request)
    body = await json_object(request)
    return created(await call(accounts.signup, body))


@router.post("/accounts/login")
@guarded
async def login(request: Request):
    _bff(request)
    body = await json_object(request)
    return await call(accounts.login, body)


@router.get("/accounts/me")
@guarded
async def me(request: Request):
    return await call(accounts.me, _user_id(request))


@router.delete("/accounts/me")
@guarded
async def delete_me(request: Request):
    # 탈퇴. 본문 {password}로 다시 확인한다
    user_id = _user_id(request)
    body = await json_object(request)
    await call(accounts.delete_account, user_id, body)
    return no_content()


@router.post("/accounts/password")
@guarded
async def change_password(request: Request):
    user_id = _user_id(request)
    body = await json_object(request)
    await call(accounts.change_password, user_id, body)
    return no_content()


@router.post("/orgs")
@guarded
async def create_org(request: Request):
    user_id = _user_id(request)
    body = await json_object(request)
    return created(await call(accounts.create_org, user_id, body))


@router.patch("/orgs/{org_id}")
@guarded
async def update_org(org_id: str, request: Request):
    user_id = _user_id(request)
    body = await json_object(request)
    return await call(accounts.update_org, user_id, org_id, body)


@router.get("/orgs/{org_id}/members")
@guarded
async def members(org_id: str, request: Request):
    return await call(accounts.list_members, _user_id(request), org_id)


@router.delete("/orgs/{org_id}/members/{member_id}")
@guarded
async def remove_member(org_id: str, member_id: str, request: Request):
    # owner는 누구든 빼고, member는 자기만 뺀다(나가기)
    await call(accounts.remove_member, _user_id(request), org_id, member_id)
    return no_content()


@router.post("/orgs/{org_id}/owners/{member_id}")
@guarded
async def make_owner(org_id: str, member_id: str, request: Request):
    return await call(accounts.make_owner, _user_id(request), org_id, member_id)


@router.post("/orgs/{org_id}/invites")
@guarded
async def create_invite(org_id: str, request: Request):
    return created(await call(accounts.create_invite, _user_id(request), org_id))


@router.get("/invites/{token}")
@guarded
async def invite(token: str, request: Request):
    # 초대 화면(로그인 전)이 초대한 조직을 보인다. 사용자 없이 BFF 키만 본다
    _bff(request)
    return await call(accounts.peek_invite, token)


@router.post("/invites/{token}/accept")
@guarded
async def accept_invite(token: str, request: Request):
    return await call(accounts.accept_invite, _user_id(request), token)


@router.get("/demo/orgs")
@guarded
async def demo_orgs(request: Request):
    _bff(request)
    return await call(accounts.demo_orgs)
