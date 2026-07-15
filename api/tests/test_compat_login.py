"""TDD suite for P5-02A login compatibility.

Covers:
  1. TestLoginUsernameAlias      - new frontend shape {username, password} works
  2. TestLoginEmailExisting      - existing/canonical shape {email, password} still works
  3. TestLoginBackwardCompatibility - precedence, missing-both, wrong-password edge cases

Written before api/compat.py exists (TDD red step) and expected to pass once
api/compat.py + the auth.py signature swap land (green step).
"""


# ── 1. Username alias ───────────────────────────────────────────────────────
class TestLoginUsernameAlias:
    def test_username_alias_logs_in_successfully(self, client, seed_user):
        user, password = seed_user

        res = client.post(
            "/api/auth/login",
            json={"username": user.email, "password": password, "turnstile_token": "ignored-token"},
        )

        assert res.status_code == 200
        body = res.json()
        assert body["code"] == 1
        assert body["data"]["token"]
        assert body["data"]["email"] == user.email
        assert body["data"]["user_id"] == user.id

    def test_username_alias_without_turnstile_token(self, client, seed_user):
        user, password = seed_user

        res = client.post("/api/auth/login", json={"username": user.email, "password": password})

        assert res.status_code == 200
        assert res.json()["data"]["token"]


# ── 2. Existing email shape (backward compatibility) ────────────────────────
class TestLoginEmailExisting:
    def test_email_shape_still_logs_in_successfully(self, client, seed_user):
        user, password = seed_user

        res = client.post("/api/auth/login", json={"email": user.email, "password": password})

        assert res.status_code == 200
        body = res.json()
        assert body["code"] == 1
        assert body["data"]["token"]
        assert body["data"]["email"] == user.email


# ── 3. Backward compatibility / edge cases ──────────────────────────────────
class TestLoginBackwardCompatibility:
    def test_both_email_and_username_present_prefers_email(self, client, db_session):
        from api.auth import hash_password
        from api.models import User

        real_user = User(email="real@example.com", password_hash=hash_password("realpass"), nickname="real")
        db_session.add(real_user)
        db_session.commit()

        # username points at an account that doesn't exist; email points at the real one.
        # AliasChoices("email", "username") must resolve "email" first.
        res = client.post(
            "/api/auth/login",
            json={"email": "real@example.com", "username": "decoy@example.com", "password": "realpass"},
        )

        assert res.status_code == 200
        assert res.json()["data"]["email"] == "real@example.com"

    def test_neither_email_nor_username_present_is_rejected(self, client):
        res = client.post("/api/auth/login", json={"password": "whatever"})

        assert res.status_code == 422

    def test_wrong_password_still_rejected_via_username_alias(self, client, seed_user):
        user, _correct_password = seed_user

        res = client.post(
            "/api/auth/login",
            json={"username": user.email, "password": "definitely-wrong"},
        )

        assert res.status_code == 200  # route returns 200 with a business-level error envelope
        body = res.json()
        assert body["code"] == -1
        assert "invalid" in body["msg"].lower()

    def test_unknown_account_still_rejected_via_username_alias(self, client):
        res = client.post(
            "/api/auth/login",
            json={"username": "nobody@example.com", "password": "whatever"},
        )

        assert res.status_code == 200
        body = res.json()
        assert body["code"] == -1
