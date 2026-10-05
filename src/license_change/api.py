"""HTTP API（标准库实现，零三方依赖）。

启动：``python -m license_change.api --host 127.0.0.1 --port 8080``

端点：
  POST   /institutions                                  登记机构与许可证 v1
  GET    /institutions
  GET    /institutions/{id}
  GET    /institutions/{id}/license-versions            许可证版本链
  POST   /institutions/{id}/authorizations              登记项目授权依赖
  POST   /institutions/{id}/appointments                登记预约
  POST   /institutions/{id}/change-requests             提交变更申请（可带 supersedes_id）
  GET    /institutions/{id}/change-requests
  GET    /change-requests/{id}
  POST   /change-requests/{id}/submit                   登记→待核验
  POST   /change-requests/{id}/impact-report            生成影响清单→处置中
  GET    /change-requests/{id}/impact-report
  POST   /change-requests/{id}/items/{item_id}/resolve  处置阻塞项
  POST   /change-requests/{id}/approve                  批准（阻塞项须清零）
  POST   /change-requests/{id}/effective                生效（原子传播）
  POST   /change-requests/{id}/withdraw                 撤回申请
  POST   /change-requests/{id}/reject                   驳回
  POST   /change-requests/{id}/archive
  GET    /change-requests/{id}/chain                    暂停/恢复/缩减/撤回替代链
  GET    /institutions/{id}/restrictions
  GET    /institutions/{id}/service-check?subject=&at=  单科目时点判定（含依据）
  GET    /institutions/{id}/references?at=              历史服务/未来禁止项分类
  GET    /rules                                         判定规则目录
  GET    /audit?target_type=&target_id=                 审计留痕
"""
from __future__ import annotations

import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .errors import DomainError, ErrorCode, HTTP_STATUS
from .repository import Store
from .service import LicenseChangeService


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "value"):
        return obj.value
    raise TypeError(f"不可序列化：{type(obj)}")


def _require(body: dict, key: str) -> Any:
    if key not in body or body[key] in (None, ""):
        raise DomainError(ErrorCode.VALIDATION, f"缺少字段：{key}")
    return body[key]


class ApiHandler(BaseHTTPRequestHandler):
    # -- 框架 -------------------------------------------------------------
    @property
    def svc(self) -> LicenseChangeService:
        return self.server.service  # type: ignore[attr-defined]

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(ErrorCode.VALIDATION, f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise DomainError(ErrorCode.VALIDATION, "请求体必须是 JSON 对象")
        return value

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        qs = {k: v[0] for k, v in parse_qs(parts.query).items()}
        path = parts.path.rstrip("/") or "/"
        body: dict[str, Any] = {}
        try:
            for pattern, verbs, handler in ROUTES:
                m = re.fullmatch(pattern, path)
                if m and method in verbs:
                    if method == "POST":
                        body = self._read_json()
                    status, payload = handler(self.svc, body, qs, **m.groupdict())
                    self._send(status, payload)
                    return
            self._send(HTTPStatus.NOT_FOUND,
                       {"error": "NOT_FOUND", "message": f"无此端点：{method} {path}"})
        except DomainError as exc:
            self._send(HTTP_STATUS[exc.code], exc.to_dict())
        except (ValueError, KeyError, TypeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "VALIDATION", "message": str(exc)})


# ---------------------------------------------------------------------------
# 端点处理函数：返回 (status, payload)
# ---------------------------------------------------------------------------
def h_register_institution(svc, body, qs):
    result = svc.register_institution(
        _require(body, "id"), _require(body, "name"),
        _require(body, "address"), _require(body, "subjects"),
        at=body.get("at"),
    )
    return 201, result


def h_list_institutions(svc, body, qs):
    return 200, {"items": [i.to_dict() for i in svc.store.list_institutions()]}


def h_get_institution(svc, body, qs, inst_id):
    inst = svc.store.get_institution(inst_id)
    current = svc.store.current_license(inst_id)
    return 200, {"institution": inst.to_dict(), "current_license": current.to_dict()}


def h_license_versions(svc, body, qs, inst_id):
    return 200, {"items": [v.to_dict() for v in svc.store.license_versions(inst_id)]}


def h_add_authorization(svc, body, qs, inst_id):
    auth = svc.add_project_authorization(
        _require(body, "id"), inst_id, _require(body, "project_id"),
        _require(body, "required_subjects"),
        _require(body, "valid_from"), _require(body, "valid_to"),
    )
    return 201, auth.to_dict()


def h_add_appointment(svc, body, qs, inst_id):
    appt = svc.add_appointment(
        _require(body, "id"), inst_id,
        _require(body, "subject"), _require(body, "scheduled_at"),
    )
    return 201, appt.to_dict()


def h_create_request(svc, body, qs, inst_id):
    req = svc.create_change_request(
        _require(body, "id"), inst_id, _require(body, "kind"),
        _require(body, "created_by"),
        body.get("payload", {}),
        expected_effective_at=body.get("expected_effective_at"),
        at=body.get("at"),
        supersedes_id=body.get("supersedes_id"),
    )
    return 201, req.to_dict()


def h_list_requests(svc, body, qs, inst_id):
    return 200, {"items": [r.to_dict() for r in svc.store.list_change_requests(inst_id)]}


def h_get_request(svc, body, qs, req_id):
    return 200, svc.store.get_change_request(req_id).to_dict()


def h_submit(svc, body, qs, req_id):
    return 200, svc.submit_for_verification(req_id, at=body.get("at")).to_dict()


def h_generate_report(svc, body, qs, req_id):
    return 201, svc.generate_impact_report(req_id, at=body.get("at")).to_dict()


def h_get_report(svc, body, qs, req_id):
    return 200, svc.store.get_report(req_id).to_dict()


def h_resolve_item(svc, body, qs, req_id, item_id):
    item = svc.resolve_blocking_item(
        req_id, item_id, _require(body, "action"),
        _require(body, "actor"), _require(body, "note"),
        new_scheduled_at=body.get("new_scheduled_at"),
        new_subject=body.get("new_subject"),
        at=body.get("at"),
    )
    return 200, item.to_dict()


def h_approve(svc, body, qs, req_id):
    req = svc.approve_request(
        req_id, _require(body, "actor"),
        expected_effective_at=body.get("expected_effective_at"),
        at=body.get("at"),
    )
    return 200, req.to_dict()


def h_effective(svc, body, qs, req_id):
    return 200, svc.apply_effective(req_id, at=body.get("at")).to_dict()


def h_withdraw(svc, body, qs, req_id):
    return 200, svc.withdraw_request(
        req_id, _require(body, "actor"), _require(body, "reason"), at=body.get("at")
    ).to_dict()


def h_reject(svc, body, qs, req_id):
    return 200, svc.reject_request(
        req_id, _require(body, "actor"), _require(body, "reason"), at=body.get("at")
    ).to_dict()


def h_archive(svc, body, qs, req_id):
    return 200, svc.archive_request(req_id, at=body.get("at")).to_dict()


def h_chain(svc, body, qs, req_id):
    return 200, svc.get_request_chain(req_id)


def h_restrictions(svc, body, qs, inst_id):
    return 200, {"items": [r.to_dict() for r in svc.store.restrictions_for(inst_id)]}


def h_service_check(svc, body, qs, inst_id):
    if "subject" not in qs or "at" not in qs:
        raise DomainError(ErrorCode.VALIDATION, "查询参数 subject 与 at 必填")
    return 200, svc.check_service(inst_id, qs["subject"], qs["at"])


def h_references(svc, body, qs, inst_id):
    if "at" not in qs:
        raise DomainError(ErrorCode.VALIDATION, "查询参数 at 必填")
    return 200, svc.classify_references(inst_id, qs["at"])


def h_rules(svc, body, qs):
    return 200, {"rules": svc.rules_catalog()}


def h_audit(svc, body, qs):
    rows = svc.store.audit_trail(qs.get("target_type"), qs.get("target_id"))
    return 200, {"items": [e.to_dict() for e in rows]}


ROUTES: list[tuple[str, set[str], Callable]] = [
    (r"/institutions", {"POST"}, h_register_institution),
    (r"/institutions", {"GET"}, h_list_institutions),
    (r"/institutions/(?P<inst_id>[^/]+)", {"GET"}, h_get_institution),
    (r"/institutions/(?P<inst_id>[^/]+)/license-versions", {"GET"}, h_license_versions),
    (r"/institutions/(?P<inst_id>[^/]+)/authorizations", {"POST"}, h_add_authorization),
    (r"/institutions/(?P<inst_id>[^/]+)/appointments", {"POST"}, h_add_appointment),
    (r"/institutions/(?P<inst_id>[^/]+)/change-requests", {"POST"}, h_create_request),
    (r"/institutions/(?P<inst_id>[^/]+)/change-requests", {"GET"}, h_list_requests),
    (r"/institutions/(?P<inst_id>[^/]+)/restrictions", {"GET"}, h_restrictions),
    (r"/institutions/(?P<inst_id>[^/]+)/service-check", {"GET"}, h_service_check),
    (r"/institutions/(?P<inst_id>[^/]+)/references", {"GET"}, h_references),
    (r"/change-requests/(?P<req_id>[^/]+)", {"GET"}, h_get_request),
    (r"/change-requests/(?P<req_id>[^/]+)/submit", {"POST"}, h_submit),
    (r"/change-requests/(?P<req_id>[^/]+)/impact-report", {"POST"}, h_generate_report),
    (r"/change-requests/(?P<req_id>[^/]+)/impact-report", {"GET"}, h_get_report),
    (r"/change-requests/(?P<req_id>[^/]+)/items/(?P<item_id>[^/]+)/resolve", {"POST"}, h_resolve_item),
    (r"/change-requests/(?P<req_id>[^/]+)/approve", {"POST"}, h_approve),
    (r"/change-requests/(?P<req_id>[^/]+)/effective", {"POST"}, h_effective),
    (r"/change-requests/(?P<req_id>[^/]+)/withdraw", {"POST"}, h_withdraw),
    (r"/change-requests/(?P<req_id>[^/]+)/reject", {"POST"}, h_reject),
    (r"/change-requests/(?P<req_id>[^/]+)/archive", {"POST"}, h_archive),
    (r"/change-requests/(?P<req_id>[^/]+)/chain", {"GET"}, h_chain),
    (r"/rules", {"GET"}, h_rules),
    (r"/audit", {"GET"}, h_audit),
]


def make_server(host: str, port: int, *, verbose: bool = False,
                service: LicenseChangeService | None = None) -> ThreadingHTTPServer:
    service = service or LicenseChangeService(Store())
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = service  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="许可证变更影响后端 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port, verbose=args.verbose)
    print(f"API 已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
