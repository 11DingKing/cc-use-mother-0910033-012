"""HTTP API 端到端测试（真实 socket，零依赖）。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from license_change.api import build_server  # noqa: E402


class ApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data if method == "POST" else None,
            method=method, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = build_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_full_suspend_flow_over_http(self) -> None:
        api = self.api
        # 机构 + 许可证 v1
        status, resp = api.call("POST", "/institutions", {
            "institution_id": "INST-A", "name": "康宁诊所",
            "address": "和平路1号", "subjects": ["内科", "口腔科"],
            "effective_from": "2026-01-01"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["data"]["license_version"]["version_no"], 1)

        # 项目与引用
        self.assertEqual(api.call("POST", "/institutions/INST-A/projects", {
            "project_id": "P1", "name": "家庭医生",
            "address_required": "和平路1号",
            "subjects_required": ["内科"]})[0], 200)
        self.assertEqual(api.call("POST", "/institutions/INST-A/bookings", {
            "booking_id": "B1", "project_id": "P1", "subject": "内科",
            "service_date": "2026-07-01", "status": "historical"})[0], 200)
        self.assertEqual(api.call("POST", "/institutions/INST-A/bookings", {
            "booking_id": "B2", "project_id": "P1", "subject": "内科",
            "service_date": "2026-08-20"})[0], 200)

        # 暂停申请 -> 受理 -> 影响清单
        status, resp = api.call("POST", "/institutions/INST-A/change-requests", {
            "kind": "suspend", "effective_date": "2026-08-01",
            "created_by": "合规员甲"})
        self.assertEqual(status, 200)
        rid = resp["data"]["change_request"]["id"]
        self.assertEqual(api.call("POST", f"/change-requests/{rid}/submit",
                                  {"by": "监管员乙"})[0], 200)
        status, resp = api.call("POST", f"/change-requests/{rid}/impact-list",
                                {"by": "监管员乙"})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(resp["data"]["blockers_total"], 2)
        self.assertFalse(resp["data"]["approvable"])
        # 历史服务 B1 不在清单
        self.assertNotIn("B1", {i["ref_id"] for i in resp["data"]["items"]})

        # 未处置阻塞项即批准 -> 409
        status, resp = api.call("POST", f"/change-requests/{rid}/approve",
                                {"by": "监管员乙"})
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"]["code"], "blockers_open")

        # 处置全部阻塞项
        _, listing = api.call("GET", f"/change-requests/{rid}/impact-list")
        for item in listing["data"]["items"]:
            action = "suspend_project" if item["ref_type"] == "project" else "cancel_booking"
            s, r = api.call("POST", f"/change-requests/{rid}/dispositions", {
                "item_id": item["id"], "action": action, "by": "合规员甲"})
            self.assertEqual(s, 200, r)

        # 批准生效
        status, resp = api.call("POST", f"/change-requests/{rid}/approve",
                                {"by": "监管员乙"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["data"]["new_version"]["status"], "suspended")

        # 版本链完整
        status, resp = api.call("GET", "/institutions/INST-A/versions")
        self.assertTrue(resp["data"]["chain_intact"])
        self.assertEqual(len(resp["data"]["versions"]), 2)

        # 替代链
        status, resp = api.call("GET", "/institutions/INST-A/chain")
        self.assertEqual(resp["data"]["chain_links"][0]["link_type"], "suspend")

        # 历史 vs 未来判定
        status, resp = api.call("GET", "/institutions/INST-A/bookings/classification")
        rows = {b["booking_id"]: b for b in resp["data"]["bookings"]}
        self.assertEqual(rows["B1"]["classification"], "historical_service")
        self.assertTrue(rows["B1"]["allowed"])
        self.assertEqual(rows["B2"]["classification"], "future_disposed_cancelled")
        self.assertFalse(rows["B2"]["allowed"])
        self.assertTrue(any("R-BOOK-DATE" in b for b in rows["B1"]["basis"]))

    def test_error_mapping(self) -> None:
        api = self.api
        self.assertEqual(api.call("GET", "/institutions/NOPE")[0], 404)
        self.assertEqual(api.call("GET", "/nope")[0], 404)
        status, resp = api.call("POST", "/institutions", {
            "institution_id": "INST-B", "name": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], "bad_request")

    def test_withdraw_blocks_later_approval(self) -> None:
        api = self.api
        api.call("POST", "/institutions", {
            "institution_id": "INST-C", "name": "仁和诊所",
            "address": "胜利路2号", "subjects": ["内科"],
            "effective_from": "2026-01-01"})
        _, resp = api.call("POST", "/institutions/INST-C/change-requests", {
            "kind": "suspend", "effective_date": "2026-08-01",
            "created_by": "合规员甲"})
        rid = resp["data"]["change_request"]["id"]
        status, resp = api.call("POST", f"/change-requests/{rid}/withdraw",
                                {"by": "合规员甲", "reason": "自主撤回"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["data"]["change_request"]["decision"], "withdrawn")
        # 撤回后受理失败
        status, resp = api.call("POST", f"/change-requests/{rid}/submit",
                                {"by": "监管员乙"})
        self.assertEqual(status, 409)
        # 许可证仍为 active
        _, resp = api.call("GET", "/institutions/INST-C")
        self.assertEqual(resp["data"]["current_version"]["status"], "active")


if __name__ == "__main__":
    unittest.main()
