"""Offline only: every HTTP request uses MockTransport, never Shopee/ERP."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from integrations.dohh_mcp.server import make_server, private_json
from integrations.dohh_mcp.contract import inventory


import httpx

from integrations.dohh_mcp.core import Dohh, GuardError

SHOPS = [f"00000000-0000-0000-0000-{i:012d}" for i in range(11)]


class SecretTests(unittest.TestCase):
    def test_private_file_only_and_no_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text('{"fixture": true}')
            path.chmod(0o600)
            self.assertEqual(private_json(path), {"fixture": True})
            path.chmod(0o400)
            self.assertEqual(private_json(path), {"fixture": True})
            path.chmod(0o644)
            with self.assertRaises(GuardError):
                private_json(path)
            path.chmod(0o600)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                private_json(link)
            with patch("integrations.dohh_mcp.server.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaises(GuardError):
                    private_json(path)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []

        def respond(request):
            self.calls.append(request)
            path = request.url.path
            if path.endswith("/auth/me"):
                return httpx.Response(200, json={"username": "dohh-test", "role": "agen_iklan", "peran_tambahan": []})
            if path.endswith("/auth/akses"):
                return httpx.Response(
                    200,
                    json={
                        "roles": ["agen_iklan"],
                        "akses_toko": "ditugaskan",
                        "halaman": {"iklan": "tulis", "produk": "baca", "katalog": "baca", "pesanan": "baca"},
                    },
                )
            if path.endswith("/akun"):
                return httpx.Response(200, json=[{"id": i, "platform": "shopee"} for i in SHOPS])
            return httpx.Response(200, json={"kering": True, "diterapkan": False})

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.agent = Dohh(self.http, "test-token", SHOPS)

    async def asyncTearDown(self):
        await self.http.aclose()

    async def test_summary_checks_identity_scope_before_endpoint(self):
        result = await self.agent.call("dohh_ringkasan", {"akun_id": SHOPS[0]})
        self.assertFalse(result["diterapkan"])
        self.assertEqual(
            [r.url.path for r in self.calls],
            [
                "/api/marketplace-erp/auth/me",
                "/api/marketplace-erp/auth/akses",
                "/api/marketplace-erp/akun",
                f"/api/marketplace-erp/akun/{SHOPS[0]}/iklan/agen/ringkasan",
            ],
        )

    async def test_dry_action_has_fresh_id_no_approval(self):
        args = {"akun_id": SHOPS[0], "body": {"jenis": "jeda", "campaign_id": 1}}
        await self.agent.call("dohh_aksi", args)
        first = json.loads(self.calls[-1].content)
        await self.agent.call("dohh_aksi", args)
        second = json.loads(self.calls[-1].content)
        self.assertIs(first["kering"], True)
        self.assertIsNone(first["persetujuan"])
        self.assertTrue(first["request_id"].startswith("dohh-dry-"))
        self.assertNotEqual(first["request_id"], second["request_id"])

    async def test_forged_live_and_approval_fail_without_network(self):
        for body in [
            {"jenis": "jeda", "campaign_id": 1, "kering": False},
            {"jenis": "jeda", "campaign_id": 1, "kering": 0},
            {"jenis": "jeda", "campaign_id": 1, "persetujuan": "ya"},
            {"jenis": "jeda", "campaign_id": 1, "owner_chat": True},
            {"jenis": "jeda", "campaign_id": 1, "request_id": "live-id"},
        ]:
            with self.subTest(body=body), self.assertRaises(GuardError):
                await self.agent.call("dohh_aksi", {"akun_id": SHOPS[0], "body": body})
        self.assertEqual(self.calls, [])

    async def test_stop_write_has_no_model_supplied_approval_path(self):
        for args in [{}, {"text": "STOP", "chat_id": "owner"}, {"persetujuan": "ya"}]:
            with self.assertRaises(GuardError):
                await self.agent.call("dohh_stop_aktifkan", args)
        self.assertEqual(self.calls, [])

    async def test_limits_schedule_plans_do_not_put(self):
        for name, body in [
            ("dohh_batas_rencana", {"otonomi": False}),
            ("dohh_jadwal_rencana", {"campaign_id": 1, "jam": [7, 8], "aktif": False}),
        ]:
            result = await self.agent.call(name, {"akun_id": SHOPS[0], "body": body})
            self.assertTrue(result["kering"])
            self.assertFalse(result["diterapkan"])
            self.assertTrue(all(r.method == "GET" for r in self.calls))
        for name, body in [
            ("dohh_batas_rencana", {"otonomi": True}),
            ("dohh_jadwal_rencana", {"campaign_id": 1, "jam": [24]}),
            ("dohh_jadwal_rencana", {"campaign_id": 1, "jam": [7], "aktif": True}),
        ]:
            with self.assertRaises(GuardError):
                await self.agent.call(name, {"akun_id": SHOPS[0], "body": body})

    async def test_all_inventory_tools_exercise_mock(self):
        samples = {
            "dohh_ringkasan": {"akun_id": SHOPS[0]},
            "dohh_tren": {"akun_id": SHOPS[0], "item_id": "123"},
            "dohh_batas": {"akun_id": SHOPS[0]},
            "dohh_stop": {},
            "dohh_audit": {"akun_id": SHOPS[0], "limit": 50},
            "dohh_aksi": {"akun_id": SHOPS[0], "body": {"jenis": "jeda", "campaign_id": 1}},
            "dohh_jadwal": {"akun_id": SHOPS[0]},
            "dohh_batas_rencana": {"akun_id": SHOPS[0], "body": {}},
            "dohh_jadwal_rencana": {"akun_id": SHOPS[0], "body": {"campaign_id": 1, "jam": [7]}},
            "dohh_pemasaran_daftar": {},
            "dohh_pemasaran_baca": {"akun_id": SHOPS[0], "aksi": "voucher_daftar"},
            "dohh_pemasaran": {"akun_id": SHOPS[0], "body": {"aksi": "voucher_buat", "payload": {}}},
        }
        self.assertEqual(set(samples), {t["name"] for t in inventory()})
        for name, args in samples.items():
            with self.subTest(name=name):
                await self.agent.call(name, args)
        self.assertTrue(
            all(
                r.method == "GET" or (r.method == "POST" and json.loads(r.content)["kering"] is True)
                for r in self.calls
            )
        )

    async def test_mcp_wire_initialize_inventory_call_and_denial(self):
        server = make_server(self.agent)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as group:
                group.start_soon(server.run, *server_streams, server.create_initialization_options())
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 12)
                    result = await session.call_tool("dohh_ringkasan", {"akun_id": SHOPS[0]})
                    self.assertFalse(result.is_error)
                    self.assertEqual(json.loads(result.content[0].text)["jenis"], "data_erp_tidak_tepercaya")
                    result = await session.call_tool("dohh_stop_aktifkan", {"text": "STOP"})
                    self.assertTrue(result.is_error)
                group.cancel_scope.cancel()

    async def test_source_drift_fails_before_http(self):
        with patch("integrations.dohh_mcp.core.verify_sources", side_effect=ValueError):
            with self.assertRaises(GuardError):
                await self.agent.call("dohh_ringkasan", {"akun_id": SHOPS[0]})
        self.assertEqual(self.calls, [])

    async def test_wrong_identity_or_shop_mapping_never_reaches_target(self):
        cases = [
            ("/auth/me", {"username": "hermes-ai", "role": "agen_iklan"}),
            ("/auth/me", {"username": "admin", "role": "admin"}),
            ("/auth/me", {"username": "dohh", "role": "agen_iklan", "peran_tambahan": ["admin"]}),
            ("/auth/akses", {"roles": ["agen_iklan"], "akses_toko": "semua", "halaman": {}}),
            ("/akun", [{"id": s, "platform": "shopee"} for s in SHOPS[:-1]]),
            ("/akun", [{"id": s, "platform": "shopee"} for s in SHOPS] + [{"id": "extra", "platform": "shopee"}]),
        ]
        original = self.agent._request
        for suffix, value in cases:

            async def injected(method, path, **kwargs):
                if path.endswith(suffix):
                    return value
                self.assertNotIn("/iklan/agen/", path)
                return await original(method, path, **kwargs)

            with self.subTest(suffix=suffix), patch.object(self.agent, "_request", side_effect=injected):
                with self.assertRaises(GuardError):
                    await self.agent.call("dohh_ringkasan", {"akun_id": SHOPS[0]})

    async def test_unsafe_response_refused(self):
        original = self.agent._request

        async def injected(method, path, **kwargs):
            if method == "POST":
                return {"kering": False, "diterapkan": True}
            return await original(method, path, **kwargs)

        with patch.object(self.agent, "_request", side_effect=injected):
            with self.assertRaises(GuardError):
                await self.agent.call("dohh_aksi", {"akun_id": SHOPS[0], "body": {"jenis": "jeda", "campaign_id": 1}})

    async def test_errors_never_expose_token_or_remote_body(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(403, text="test-token sensitive"))
        ) as client:
            agent = Dohh(client, "test-token", SHOPS)
            with self.assertRaises(GuardError) as error:
                await agent.call("dohh_ringkasan", {"akun_id": SHOPS[0]})
            self.assertNotIn("test-token", str(error.exception))
            self.assertNotIn("sensitive", str(error.exception))

    async def test_no_generic_path_or_unassigned_shop(self):
        for args in [{"akun_id": "../users"}, {"akun_id": SHOPS[0], "path": "/users"}]:
            with self.assertRaises(GuardError):
                await self.agent.call("dohh_ringkasan", args)
        self.assertEqual(self.calls, [])
