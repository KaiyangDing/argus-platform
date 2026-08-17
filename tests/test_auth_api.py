"""auth API 集成测试：走真 PG（argus_test 库），覆盖 P1.2 验收行。"""

from httpx import AsyncClient, Response

EMAIL = "alice@example.com"
PASSWORD = "password-123"


async def _register(
    client: AsyncClient, email: str = EMAIL, password: str = PASSWORD
) -> Response:
    return await client.post(
        "/api/auth/register", json={"email": email, "password": password}
    )


async def test_register_returns_token_pair(client: AsyncClient) -> None:
    resp = await _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]


async def test_register_duplicate_email_409(client: AsyncClient) -> None:
    await _register(client)
    resp = await _register(client, password="another-pass-456")
    assert resp.status_code == 409


async def test_register_short_password_422(client: AsyncClient) -> None:
    resp = await _register(client, password="short")
    assert resp.status_code == 422


async def test_login_then_me(client: AsyncClient) -> None:
    await _register(client)
    resp = await client.post(
        "/api/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    assert resp.status_code == 200
    access = resp.json()["access_token"]
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL


async def test_login_wrong_password_401(client: AsyncClient) -> None:
    await _register(client)
    resp = await client.post(
        "/api/auth/login", json={"email": EMAIL, "password": "totally-wrong"}
    )
    assert resp.status_code == 401


async def test_login_unknown_email_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 401


async def test_email_case_insensitive(client: AsyncClient) -> None:
    await _register(client, email="Alice@Example.com")
    resp = await client.post(
        "/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 200


async def test_me_without_token_401(client: AsyncClient) -> None:
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_with_refresh_token_401(client: AsyncClient) -> None:
    pair = (await _register(client)).json()
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {pair['refresh_token']}"}
    )
    assert resp.status_code == 401


async def test_refresh_issues_working_access(client: AsyncClient) -> None:
    pair = (await _register(client)).json()
    resp = await client.post(
        "/api/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert resp.status_code == 200
    new_access = resp.json()["access_token"]
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert me.status_code == 200


async def test_refresh_with_access_token_401(client: AsyncClient) -> None:
    pair = (await _register(client)).json()
    resp = await client.post(
        "/api/auth/refresh", json={"refresh_token": pair["access_token"]}
    )
    assert resp.status_code == 401
