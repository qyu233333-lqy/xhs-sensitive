from unittest.mock import patch

from app import create_app


def _auth_config(**overrides):
    auth = {
        "enabled": True,
        "mode": "in_app",
        "dingtalk_corp_id": "dingcorp123",
        "dingtalk_agent_id": "123456",
        "dingtalk_app_key": "app_key",
        "dingtalk_app_secret": "app_secret",
        "dingtalk_redirect_uri": "",
        "dingtalk_scope": "openid",
        "authorize_url": "https://login.dingtalk.com/oauth2/auth",
        "app_access_token_url": "https://api.dingtalk.com/v1.0/oauth2/accessToken",
        "user_access_token_url": "https://api.dingtalk.com/v1.0/oauth2/userAccessToken",
        "user_info_url": "https://api.dingtalk.com/v1.0/contact/users/me",
        "user_info_by_code_url": "https://oapi.dingtalk.com/topapi/v2/user/getuserinfo",
        "user_detail_url": "https://oapi.dingtalk.com/topapi/v2/user/get",
        "user_mapping_path": "user_groups.json",
    }
    auth.update(overrides)
    return auth


def _base_config(auth_overrides=None):
    return {
        "session_secret": "test-secret",
        "auth": _auth_config(**(auth_overrides or {})),
        "default_profile_id": "ops1",
        "key_profiles": {
            "ops1": {
                "label": "运营一部",
                "api_key": "test-key",
                "base_url": "https://4sapi.com",
                "model": "claude-sonnet-4-6",
                "enabled": True,
            }
        },
    }


def test_auth_me_reports_in_app_mode_metadata():
    app = create_app()
    client = app.test_client()

    with patch("routes.api.load_config", return_value=_base_config()):
        resp = client.get("/api/auth/me")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["mode"] == "in_app"
    assert data["corp_id"] == "dingcorp123"
    assert data["authenticated"] is False


def test_in_app_login_sets_authenticated_session():
    app = create_app()
    client = app.test_client()
    auth_user = {
        "authenticated": True,
        "profile_id": "ops1",
        "profile_label": "运营一部",
        "display_name": "张三",
        "is_admin": False,
        "identifiers": {"userid": "user123"},
    }

    with patch("routes.api.load_config", return_value=_base_config()), \
         patch("routes.api.fetch_dingtalk_user_info_by_code", return_value={"userid": "user123", "name": "张三"}), \
         patch("routes.api.resolve_user_access", return_value=auth_user):
        resp = client.post("/api/auth/dingtalk/in-app-login", json={"auth_code": "tmp-code"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["user"]["profile_id"] == "ops1"

    with client.session_transaction() as session:
        assert session["auth_user"]["display_name"] == "张三"


def test_in_app_login_rejects_oauth_mode():
    app = create_app()
    client = app.test_client()

    with patch("routes.api.load_config", return_value=_base_config({"mode": "oauth", "dingtalk_redirect_uri": "http://localhost/callback"})):
        resp = client.post("/api/auth/dingtalk/in-app-login", json={"auth_code": "tmp-code"})

    assert resp.status_code == 400
    assert "未启用钉钉内免登模式" in resp.get_json()["error"]
