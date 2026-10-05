"""JSON HTTP API（标准库实现，零依赖）。

路由：
- POST   /institutions                                 登记机构（产生 v1 许可证版本）
- POST   /institutions/{id}/projects                   登记项目授权
- POST   /institutions/{id}/bookings                   登记预约/历史服务
- GET    /institutions/{id}                            机构当前状态
- GET    /institutions/{id}/versions                   许可证版本链
- GET    /institutions/{id}/chain                      暂停/恢复/缩减/撤回替代链
- POST   /institutions/{id}/change-requests            提交变更申请
- GET    /institutions/{id}/change-requests            申请列表
- POST   /change-requests/{rid}/submit                 登记 -> 待核验
- POST   /change-requests/{rid}/impact-list            生成影响清单（-> 处置中）
- GET    /change-requests/{rid}/impact-list            查看清单与可否批准
- POST   /change-requests/{rid}/dispositions           处置阻塞项
- POST   /change-requests/{rid}/approve                批准并原子生效
- POST   /change-requests/{rid}/reject                 驳回
- POST   /change-requests/{rid}/withdraw               撤回
- POST   /change-requests/{rid}/archive                归档
- GET    /institutions/{id}/bookings/classification    历史服务 vs 未来禁止项（含依据）
- GET    /bookings/{bid}/classification
"""
from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import DomainError
from .models import (
    BookingStatus,
    ChangeKind,
    DispositionAction,
)
from .service import LicenseChangeService

CHANGE_KINDS = {k.value: k for k in ChangeKind}
DISPOSITION_ACTIONS = {a.value: a for a in DispositionAction}
BOOKING_STATUSES = {s.value: s for s in BookingStatus}


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_date", f"{field} 必须是 ISO 日期（YYYY-MM-DD）")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def create_handler(service: LicenseChangeService) -> type[BaseHTTPRequestHandler]:
    routes: list[tuple[re.Pattern[str], str, Callable[..., Any]]] = []

    def route(pattern: str, method: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            routes.append((re.compile(f"^{pattern}$"), method, fn))
            return fn
        return deco

    # ------------------------------------------------------------ 机构与引用

    @route(r"/institutions", "POST")
    def create_institution(body: dict[str, Any]) -> dict[str, Any]:
        missing = {"institution_id", "name", "address", "subjects"} - body.keys()
        if missing:
            raise ApiError(400, "bad_request", f"缺少字段：{sorted(missing)}")
        eff = _parse_date(body["effective_from"], "effective_from") if body.get("effective_from") else None
        version = service.register_institution(
            body["institution_id"], body["name"], body["address"],
            set(body["subjects"]), eff,
        )
        return {"institution": {"id": version.institution_id}, "license_version": version.to_dict()}

    @route(r"/institutions/(?P<inst>[\w-]+)", "GET")
    def get_institution(body: dict[str, Any], inst: str) -> dict[str, Any]:
        inst_agg = service._institution(inst)  # noqa: SLF001 - 同包 API 组合
        current = service.current_version(inst)
        return {
            "institution": {
                "id": inst_agg.id, "name": inst_agg.name,
                "address": inst_agg.address, "subjects": list(inst_agg.subjects),
                "status": current.status.value,
            },
            "current_version": current.to_dict(),
        }

    @route(r"/institutions/(?P<inst>[\w-]+)/projects", "POST")
    def create_project(body: dict[str, Any], inst: str) -> dict[str, Any]:
        missing = {"project_id", "name", "address_required", "subjects_required"} - body.keys()
        if missing:
            raise ApiError(400, "bad_request", f"缺少字段：{sorted(missing)}")
        project = service.add_project(
            body["project_id"], inst, body["name"],
            body["address_required"], set(body["subjects_required"]),
        )
        return {"project": project.to_dict()}

    @route(r"/institutions/(?P<inst>[\w-]+)/bookings", "POST")
    def create_booking(body: dict[str, Any], inst: str) -> dict[str, Any]:
        missing = {"booking_id", "project_id", "subject", "service_date"} - body.keys()
        if missing:
            raise ApiError(400, "bad_request", f"缺少字段：{sorted(missing)}")
        status = BookingStatus.FUTURE
        if body.get("status"):
            if body["status"] not in BOOKING_STATUSES:
                raise ApiError(400, "bad_request", f"未知预约状态：{body['status']}")
            status = BOOKING_STATUSES[body["status"]]
        booking = service.add_booking(
            body["booking_id"], inst, body["project_id"], body["subject"],
            _parse_date(body["service_date"], "service_date"), status,
        )
        return {"booking": booking.to_dict()}

    @route(r"/institutions/(?P<inst>[\w-]+)/versions", "GET")
    def list_versions(body: dict[str, Any], inst: str) -> dict[str, Any]:
        versions = service.list_versions(inst)
        return {
            "institution_id": inst,
            "chain_intact": all(
                versions[i].version_no == versions[i - 1].version_no + 1
                and versions[i].prev_id == versions[i - 1].version_no
                for i in range(1, len(versions))
            ),
            "versions": [v.to_dict() for v in versions],
        }

    @route(r"/institutions/(?P<inst>[\w-]+)/chain", "GET")
    def get_chain(body: dict[str, Any], inst: str) -> dict[str, Any]:
        return {"institution_id": inst, "chain_links": service.get_chain(inst)}

    # ------------------------------------------------------------ 变更申请

    @route(r"/institutions/(?P<inst>[\w-]+)/change-requests", "POST")
    def create_request(body: dict[str, Any], inst: str) -> dict[str, Any]:
        missing = {"kind", "effective_date", "created_by"} - body.keys()
        if missing:
            raise ApiError(400, "bad_request", f"缺少字段：{sorted(missing)}")
        if body["kind"] not in CHANGE_KINDS:
            raise ApiError(400, "bad_request", f"未知变更类型：{body['kind']}")
        req = service.create_change_request(
            inst, CHANGE_KINDS[body["kind"]], body.get("proposed", {}),
            _parse_date(body["effective_date"], "effective_date"),
            body["created_by"],
        )
        return {"change_request": req.to_dict()}

    @route(r"/institutions/(?P<inst>[\w-]+)/change-requests", "GET")
    def list_requests(body: dict[str, Any], inst: str) -> dict[str, Any]:
        return {"change_requests": [r.to_dict() for r in service.list_requests(inst)]}

    @route(r"/change-requests/(?P<rid>[\w-]+)/submit", "POST")
    def submit_request(body: dict[str, Any], rid: str) -> dict[str, Any]:
        return {"change_request": service.submit_for_verification(rid, body.get("by", "")).to_dict()}

    @route(r"/change-requests/(?P<rid>[\w-]+)/impact-list", "POST")
    def generate_impact(body: dict[str, Any], rid: str) -> dict[str, Any]:
        service.generate_impact_list(rid, body.get("by", ""))
        return service.impact_list(rid)

    @route(r"/change-requests/(?P<rid>[\w-]+)/impact-list", "GET")
    def get_impact(body: dict[str, Any], rid: str) -> dict[str, Any]:
        return service.impact_list(rid)

    @route(r"/change-requests/(?P<rid>[\w-]+)/dispositions", "POST")
    def dispose(body: dict[str, Any], rid: str) -> dict[str, Any]:
        missing = {"item_id", "action", "by"} - body.keys()
        if missing:
            raise ApiError(400, "bad_request", f"缺少字段：{sorted(missing)}")
        if body["action"] not in DISPOSITION_ACTIONS:
            raise ApiError(400, "bad_request", f"未知处置动作：{body['action']}")
        payload = dict(body.get("payload", {}))
        if "new_date" in payload:
            payload["new_date"] = _parse_date(payload["new_date"], "payload.new_date")
        item = service.dispose_blocker(
            rid, body["item_id"], DISPOSITION_ACTIONS[body["action"]],
            body["by"], body.get("note", ""), payload,
        )
        return {"impact_item": item.to_dict(), "impact_list": service.impact_list(rid)}

    @route(r"/change-requests/(?P<rid>[\w-]+)/approve", "POST")
    def approve(body: dict[str, Any], rid: str) -> dict[str, Any]:
        req = service.approve_change(rid, body.get("by", ""))
        return {
            "change_request": req.to_dict(),
            "new_version": service.list_versions(req.institution_id)[-1].to_dict(),
        }

    @route(r"/change-requests/(?P<rid>[\w-]+)/reject", "POST")
    def reject(body: dict[str, Any], rid: str) -> dict[str, Any]:
        return {"change_request": service.reject_change(
            rid, body.get("by", ""), body.get("reason", "")).to_dict()}

    @route(r"/change-requests/(?P<rid>[\w-]+)/withdraw", "POST")
    def withdraw(body: dict[str, Any], rid: str) -> dict[str, Any]:
        return {"change_request": service.withdraw_request(
            rid, body.get("by", ""), body.get("reason", "")).to_dict()}

    @route(r"/change-requests/(?P<rid>[\w-]+)/archive", "POST")
    def archive(body: dict[str, Any], rid: str) -> dict[str, Any]:
        return {"change_request": service.archive_request(rid).to_dict()}

    # ------------------------------------------------------------ 历史/未来判定

    @route(r"/institutions/(?P<inst>[\w-]+)/bookings/classification", "GET")
    def classify_all(body: dict[str, Any], inst: str) -> dict[str, Any]:
        today = date.today()
        rows = service.list_bookings_classified(inst, today)
        return {"institution_id": inst, "as_of": today.isoformat(), "bookings": rows}

    @route(r"/bookings/(?P<bid>[\w-]+)/classification", "GET")
    def classify_one(body: dict[str, Any], bid: str) -> dict[str, Any]:
        return service.classify_booking(bid)

    class Handler(BaseHTTPRequestHandler):
        server_version = "LicenseChangeAPI/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    raise ApiError(400, "bad_json", "请求体不是合法 JSON")
                if not isinstance(body, dict):
                    raise ApiError(400, "bad_request", "请求体必须是 JSON 对象")

                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                for pattern, verb, fn in routes:
                    if verb != method:
                        continue
                    match = pattern.match(path)
                    if match:
                        result = fn(body, **match.groupdict())
                        self._send(200, {"ok": True, "data": result})
                        return
                raise ApiError(404, "not_found", f"无此路由：{method} {path}")
            except ApiError as exc:
                self._send(exc.status, {"ok": False, "error": {"code": exc.code, "message": str(exc)}})
            except DomainError as exc:
                status = 404 if exc.code == "not_found" else 409
                self._send(status, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
            except Exception as exc:  # noqa: BLE001 - 兜底，返回结构化 500
                self._send(500, {"ok": False, "error": {
                    "code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

    return Handler


def build_server(host: str = "127.0.0.1", port: int = 8080,
                 service: LicenseChangeService | None = None) -> ThreadingHTTPServer:
    service = service or LicenseChangeService()
    return ThreadingHTTPServer((host, port), create_handler(service))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="许可证变更影响 API 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = build_server(args.host, args.port)
    print(f"许可证变更影响 API 监听 http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
