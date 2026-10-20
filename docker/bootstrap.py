"""Complete local owner onboarding and verify the installed integration's form."""
import asyncio
import json
import os
from pathlib import Path

import aiohttp

BASE = "http://web"
CLIENT = f"http://localhost:{int(os.environ.get("HA_DEV_PORT", "18123"))}/"


async def bootstrap() -> None:
    credentials = json.loads(Path("/state/credentials.json").read_text())
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async def request(method, path, **kwargs):
            async with session.request(method, BASE + path, **kwargs) as response:
                if response.status >= 400:
                    raise RuntimeError(f"Development HA {method} {path}: HTTP {response.status}")
                body = await response.text()
                return json.loads(body) if body else {}

        async with session.get(BASE + "/api/onboarding") as response:
            steps = await response.json() if response.status == 200 else []
            if response.status not in (200, 404):
                raise RuntimeError(f"Onboarding status: HTTP {response.status}")
        if any(step["step"] == "user" and not step["done"] for step in steps):
            result = await request("POST", "/api/onboarding/users", json={
                **credentials, "name": "Developer", "client_id": CLIENT, "language": "en",
            })
            code = result["auth_code"]
        else:
            flow = await request("POST", "/auth/login_flow", json={
                "client_id": CLIENT, "handler": ["homeassistant", None], "redirect_uri": CLIENT,
            })
            result = await request("POST", "/auth/login_flow/" + flow["flow_id"], json={
                **credentials, "client_id": CLIENT,
            })
            if result["type"] != "create_entry":
                raise RuntimeError("Saved development login failed; credentials were not reset.")
            code = result["result"]
        token = await request("POST", "/auth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLIENT,
        })
        headers = {"Authorization": "Bearer " + token["access_token"]}
        entries = await request("GET", "/api/config/config_entries/entry", headers=headers)
        configured = any(entry["domain"] == "vimar_intercom" for entry in entries)
        if not configured:
            # This initial form does not probe SIP. Delete it without submitting
            # credentials, so no unfinished flow remains in the user's dashboard.
            flow = await request("POST", "/api/config/config_entries/flow", headers=headers,
                                 json={"handler": "vimar_intercom", "show_advanced_options": True})
            if flow.get("type") != "form" or flow.get("step_id") != "user":
                raise RuntimeError("Vimar setup form did not load.")
            await request("DELETE", "/api/config/config_entries/flow/" + flow["flow_id"], headers=headers)
        async with session.ws_connect(BASE + "/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token["access_token"]})
            if (await ws.receive_json())["type"] != "auth_ok":
                raise RuntimeError("Development frontend WebSocket authentication failed.")
            await ws.send_json({"id": 1, "type": "lovelace/config", "url_path": "vimar-dev"})
            if not (await ws.receive_json()).get("success"):
                raise RuntimeError("Development dashboard did not load.")
        # Revoke this short-lived bootstrap session; browser login uses its own.
        await request("POST", "/auth/token", data={
            "action": "revoke", "token": token["refresh_token"],
        })
    print("Ready: " + CLIENT + "vimar-dev/development")
    print("Username: " + credentials["username"])
    print("Password: " + credentials["password"])
    print("Credentials saved in .dev-ha/credentials.json (private, ignored by Git).")
    print("Existing Vimar configuration retained; bootstrap made no calls." if configured else
          "Vimar setup verified. No intercom registration or calls made.")


if __name__ == "__main__":
    asyncio.run(bootstrap())
