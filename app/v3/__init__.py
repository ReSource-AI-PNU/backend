"""설계안 v3 구현.

이 패키지의 핵심 모듈(evidence, judge, a2a, agents, intake, service)은 표준 라이브러리만 쓴다.
FastAPI, pydantic, SQLAlchemy, MCP SDK에 기대는 코드는 api.py, sql.py, mcp_server.py,
vendor_service.py 같은 가장자리 모듈에만 둔다. 계약은 docs/v3/CONTRACT.md에 있다.
"""
