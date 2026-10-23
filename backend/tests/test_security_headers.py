"""Response security headers (defense in depth). The API returns JSON, but
nosniff still matters, and secret-bearing responses must never be cached."""


async def test_nosniff_on_all_responses(api_env) -> None:
    response = await api_env.client.get("/livez")
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_checkout_response_is_not_cacheable(api_env) -> None:
    org_id = await api_env.create_org("hdr")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    created = await api_env.client.post(
        "/api/v1/credentials",
        json={"name": "c", "type": "windows_admin", "secret": "s3cr3t"},
        headers=headers,
    )
    cred_id = created.json()["id"]
    checkout = await api_env.client.post(
        f"/api/v1/credentials/{cred_id}/checkout",
        json={"reason": "test"},
        headers=headers,
    )
    assert checkout.status_code == 200
    # the plaintext secret must not sit in any cache
    assert checkout.headers["Cache-Control"] == "no-store"
    assert checkout.headers.get("Pragma") == "no-cache"


async def test_ordinary_responses_not_forced_no_store(api_env) -> None:
    org_id = await api_env.create_org("hdr")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    listing = await api_env.client.get("/api/v1/credentials", headers=headers)
    # a non-secret listing does not carry the no-store directive
    assert listing.headers.get("Cache-Control") != "no-store"
