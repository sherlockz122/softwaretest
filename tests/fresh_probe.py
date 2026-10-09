"""CLI probe for a disposable full environment, without printing credentials/tokens."""

import argparse
import time

import httpx

from packages.auth.bootstrap import BootstrapSettings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    settings = BootstrapSettings(_env_file=args.config)
    with httpx.Client(base_url=args.base_url, trust_env=False, timeout=10) as api:
        login = api.post(
            "/api/v1/auth/login",
            json={
                "username": settings.bootstrap_username,
                "password": settings.bootstrap_password.get_secret_value(),
            },
            headers={"Origin": args.base_url},
        )
        assert login.status_code == 201, "Fresh login failed"
        headers = {
            "Authorization": "Bearer " + login.json()["access_token"],
            "Idempotency-Key": "fresh-initialization",
        }
        assert api.get("/api/v1/users/me", headers=headers).status_code == 200
        task = api.post("/api/v1/tasks/diagnostic", json={"duration_seconds": 0}, headers=headers)
        assert task.status_code == 202, "Fresh task creation failed"
        task_id = task.json()["task_id"]
        assert (
            api.post(
                "/api/v1/tasks/diagnostic", json={"duration_seconds": 0}, headers=headers
            ).json()["task_id"]
            == task_id
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = api.get("/api/v1/tasks/" + task_id, headers=headers)
            if result.status_code == 200 and result.json()["status"] == "succeeded":
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Fresh real-worker task did not finish")
        refresh = api.post(
            "/api/v1/auth/refresh",
            headers={"Origin": args.base_url, "X-CSRF-Token": login.json()["csrf_token"]},
        )
        assert refresh.status_code == 200
        assert (
            api.post(
                "/api/v1/auth/logout",
                headers={"Origin": args.base_url, "X-CSRF-Token": refresh.json()["csrf_token"]},
            ).status_code
            == 204
        )
        assert api.get("/api/v1/users/me", headers=headers).status_code == 401
    print("PASS fresh config/migration/seed, HTTP auth and real diagnostic execution")


if __name__ == "__main__":
    main()
