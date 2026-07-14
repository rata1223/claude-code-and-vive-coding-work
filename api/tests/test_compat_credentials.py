"""TDD suite for P5-02A credential compatibility.

Covers:
  1. TestCredentialRequestMapping      - frontend shape maps onto the real columns
  2. TestCredentialResponseMapping     - response envelope contract is unchanged (masked, no leak)
  3. TestCredentialBackwardCompatibility - old/direct backend-native shape still works
  4. TestRegressionExistingEndpoints   - untouched endpoints keep working exactly as before

Written before api/compat.py exists (TDD red step) and expected to pass once
api/compat.py + the credentials.py signature swap land (green step).
"""
from api.crypto import decrypt
from api.models import Credential


# ── 1. Request mapping ──────────────────────────────────────────────────────
class TestCredentialRequestMapping:
    def test_frontend_shape_maps_api_key_and_secret_key_onto_app_columns(self, client, auth_headers, db_session):
        res = client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={
                "name": "My KIS Account",
                "exchange_id": "kis",
                "api_key": "plain-app-key-123",
                "secret_key": "plain-app-secret-456",
                "passphrase": "should-be-ignored",
                "account_no": "12345678-01",
                "hts_id": "myhtsid",
                "enable_demo_trading": True,
            },
        )

        assert res.status_code == 200
        assert res.json()["code"] == 1

        cred = db_session.query(Credential).order_by(Credential.id.desc()).first()
        assert cred is not None
        assert decrypt(cred.app_key_enc) == "plain-app-key-123"
        assert decrypt(cred.app_secret_enc) == "plain-app-secret-456"
        assert cred.env == "paper"
        assert decrypt(cred.account_no_enc) == "12345678-01"
        assert decrypt(cred.hts_id_enc) == "myhtsid"
        # The distinct legacy `api_key` column must NOT also receive the
        # frontend's app-key value — regression guard for the alias-overlap
        # bug where both `app_key` and `api_key` independently bound to the
        # same incoming "api_key" JSON key.
        assert cred.api_key_enc is None

    def test_enable_demo_trading_false_maps_to_real_env(self, client, auth_headers, db_session):
        res = client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={
                "name": "Live Account",
                "exchange_id": "kis",
                "api_key": "k",
                "secret_key": "s",
                "enable_demo_trading": False,
            },
        )

        assert res.status_code == 200
        cred = db_session.query(Credential).order_by(Credential.id.desc()).first()
        assert cred.env == "real"

    def test_passphrase_is_accepted_but_not_persisted_anywhere(self, client, auth_headers, db_session):
        res = client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={
                "name": "Acct",
                "exchange_id": "kis",
                "api_key": "k",
                "secret_key": "s",
                "passphrase": "super-secret-passphrase-value",
            },
        )

        assert res.status_code == 200
        cred = db_session.query(Credential).order_by(Credential.id.desc()).first()
        # No column exists to hold it, and it must not have leaked into any other encrypted field.
        assert decrypt(cred.app_key_enc) == "k"
        assert decrypt(cred.app_secret_enc) == "s"
        assert decrypt(cred.account_no_enc) != "super-secret-passphrase-value"
        assert decrypt(cred.hts_id_enc) != "super-secret-passphrase-value"


# ── 2. Response mapping ─────────────────────────────────────────────────────
class TestCredentialResponseMapping:
    def test_create_response_masks_sensitive_fields(self, client, auth_headers):
        res = client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={
                "name": "My KIS Account",
                "exchange_id": "kis",
                "api_key": "plain-app-key-123",
                "secret_key": "plain-app-secret-456",
                "account_no": "12345678-01",
                "hts_id": "myhtsid",
                "enable_demo_trading": True,
            },
        )

        data = res.json()["data"]
        assert data["name"] == "My KIS Account"
        assert data["exchange_id"] == "kis"
        assert data["env"] == "paper"
        assert data["app_key"] == "****"
        assert data["account_no"] == "****"
        assert data["hts_id"] == "****"
        assert "plain-app-key-123" not in res.text
        assert "plain-app-secret-456" not in res.text


# ── 3. Backward compatibility ───────────────────────────────────────────────
class TestCredentialBackwardCompatibility:
    def test_old_backend_native_shape_still_works_unchanged(self, client, auth_headers, db_session):
        res = client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={
                "name": "Direct Shape",
                "exchange_id": "kis",
                "app_key": "direct-app-key",
                "app_secret": "direct-app-secret",
                "account_no": "999",
                "hts_id": "hts999",
                "env": "real",
            },
        )

        assert res.status_code == 200
        cred = db_session.query(Credential).order_by(Credential.id.desc()).first()
        assert decrypt(cred.app_key_enc) == "direct-app-key"
        assert decrypt(cred.app_secret_enc) == "direct-app-secret"
        assert cred.env == "real"


# ── 4. Regression: untouched endpoints ──────────────────────────────────────
class TestRegressionExistingEndpoints:
    def test_register_endpoint_unaffected(self, client):
        res = client.post(
            "/api/auth/register",
            json={"email": "newuser@example.com", "password": "sixchars", "nickname": "newbie"},
        )

        assert res.status_code == 200
        body = res.json()
        assert body["code"] == 1
        assert body["data"]["email"] == "newuser@example.com"

    def test_credentials_list_unaffected(self, client, auth_headers):
        client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={"name": "A", "exchange_id": "kis", "app_key": "k1", "app_secret": "s1"},
        )

        res = client.get("/api/credentials/list", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["code"] == 1
        assert len(res.json()["data"]["items"]) >= 1

    def test_credentials_get_unaffected(self, client, auth_headers):
        create_res = client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={"name": "A", "exchange_id": "kis", "app_key": "k1", "app_secret": "s1"},
        )
        cred_id = create_res.json()["data"]["id"]

        res = client.get(f"/api/credentials/get?id={cred_id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["data"]["id"] == cred_id

    def test_credentials_delete_unaffected(self, client, auth_headers):
        create_res = client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={"name": "A", "exchange_id": "kis", "app_key": "k1", "app_secret": "s1"},
        )
        cred_id = create_res.json()["data"]["id"]

        res = client.delete(f"/api/credentials/delete?id={cred_id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["code"] == 1
