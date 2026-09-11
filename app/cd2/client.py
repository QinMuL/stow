"""CloudDrive2 gRPC 客户端(上传段):连接/认证/文件操作/上传任务。

同步调用 gRPC(与旧项目一致),异步侧用 `asyncio.to_thread` 包裹;连接态随实例持有。

认证:优先 API 令牌(`authorization: Bearer <token>`),其次账号密码 `GetToken` 换 JWT。
上传语义:CD2 的"上传"是 **CopyFile(跨云复制)** —— 从 CD2 挂载视图里的本地目录
复制到目标云盘目录;冲突策略 Skip/DestExists 时不会重复传(配合 115 秒传,命中则秒级完成)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ConflictPolicy / TaskStatus(proto 枚举实测值)
CONFLICT_OVERWRITE, CONFLICT_RENAME, CONFLICT_SKIP = 0, 1, 2
TASK_PENDING, TASK_RUNNING, TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED = 0, 1, 2, 3, 4
TASK_MODE_COPY, TASK_MODE_MOVE = 0, 1   # 实测:本机服务器 1.0.13 上 move 任务 taskMode=1


@dataclass
class Cd2File:
    """CD2 文件条目(只取我们用得到的字段)。"""

    name: str
    path: str
    is_dir: bool
    size: int


class Cd2Error(Exception):
    """CD2 调用失败。"""


class Cd2Client:
    def __init__(self, address: str, token: str = "", *, username: str = "",
                 password: str = "", timeout: float = 30.0) -> None:
        self.address = (address or "").strip()
        self.token = token or ""
        self.username = username
        self.password = password
        self.timeout = timeout
        self._jwt: str | None = None
        self._channel = None
        self._stub = None

    # ── 连接与认证 ──────────────────────────────────────────
    def ensure_conn(self) -> None:
        if self._stub is not None:
            return
        if not self.address:
            raise Cd2Error("未配置 CD2 地址")
        try:
            import grpc

            from app.cd2 import clouddrive_pb2_grpc

            self._channel = grpc.insecure_channel(self.address)
            self._stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(self._channel)
        except Exception as exc:  # noqa: BLE001
            raise Cd2Error(f"连接 CD2 失败({self.address}):{exc}") from exc

    @property
    def auth_md(self) -> list[tuple[str, str]]:
        tok = self.token or self._jwt
        return [("authorization", f"Bearer {tok}")] if tok else []

    def login(self) -> None:
        """无 API 令牌时用账号密码换 JWT;都没有则报错。"""
        if self.token or self._jwt:
            return
        if not (self.username and self.password):
            raise Cd2Error("未配置 CD2 令牌或账号密码")
        pb2 = self._pb2()
        self.ensure_conn()
        resp = self._stub.GetToken(
            pb2.GetTokenRequest(userName=self.username, password=self.password),
            timeout=self.timeout)
        if not resp.success:
            raise Cd2Error(f"CD2 登录失败:{resp.errorMessage or '未知原因'}")
        self._jwt = resp.token

    def _pb2(self):
        from app.cd2 import clouddrive_pb2

        return clouddrive_pb2

    @staticmethod
    def _empty():
        from google.protobuf import empty_pb2

        return empty_pb2.Empty()

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 CD2 channel 失败:%s", exc)
            self._channel = None
            self._stub = None

    def _call(self, name: str, request, *, auth: bool = True):
        self.ensure_conn()
        md = self.auth_md if auth else []
        try:
            return getattr(self._stub, name)(request, metadata=md or None, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise Cd2Error(f"CD2 {name} 失败:{exc}") from exc

    # ── 只读(连通与路径核实用) ──────────────────────────────
    def system_info(self) -> dict:
        """免认证:服务器是否已登录 cloudfs、用户与版本。"""
        r = self._call("GetSystemInfo", self._empty(), auth=False)
        return {"logged_in": bool(r.IsLogin), "user_name": r.UserName or "",
                "ready": bool(getattr(r, "SystemReady", False)),
                "message": (getattr(r, "SystemMessage", "") or "")[:120]}

    def token_info(self, token: str = "") -> dict:
        """免认证:校验 API 令牌(返回权限与根目录限制)。"""
        from google.protobuf import wrappers_pb2

        r = self._call("GetApiTokenInfo",
                       wrappers_pb2.StringValue(value=token or self.token), auth=False)
        perms = getattr(r, "permissions", None)
        granted = []
        if perms is not None:
            for f in perms.DESCRIPTOR.fields:
                if bool(getattr(perms, f.name, False)):
                    granted.append(f.name.removeprefix("allow_"))
        return {"root_dir": getattr(r, "rootDir", "") or "",
                "friendly_name": getattr(r, "friendly_name", "") or "",
                "granted": granted,
                "can": lambda name: bool(getattr(perms, f"allow_{name}", False)) if perms else False,
                "raw": str(r)[:160]}

    def mount_points(self) -> list[str]:
        r = self._call("GetMountPoints", self._empty())
        return [m.mountPoint for m in (getattr(r, "mountPoints", []) or [])]

    def sub_files(self, path: str, *, refresh: bool = False) -> list[Cd2File]:
        """列目录(服务端流式)。"""
        pb2 = self._pb2()
        self.ensure_conn()
        try:
            stream = self._stub.GetSubFiles(
                pb2.ListSubFileRequest(path=path, forceRefresh=refresh),
                metadata=self.auth_md or None, timeout=self.timeout)
            out: list[Cd2File] = []
            for reply in stream:
                for f in getattr(reply, "subFiles", []) or []:
                    out.append(Cd2File(name=f.name, path=f.fullPathName,
                                       is_dir=bool(f.isDirectory), size=int(f.size or 0)))
            return out
        except Exception as exc:  # noqa: BLE001
            raise Cd2Error(f"CD2 列目录失败({path}):{exc}") from exc

    def find_file(self, path: str) -> Cd2File | None:
        pb2 = self._pb2()
        try:
            f = self._call("FindFileByPath", pb2.FindFileByPathRequest(path=path))
        except Cd2Error:
            return None
        if f is None or not getattr(f, "name", ""):
            return None
        return Cd2File(name=f.name, path=getattr(f, "fullPathName", "") or path,
                       is_dir=bool(f.isDirectory), size=int(f.size or 0))

    # ── 写操作(上传段用) ───────────────────────────────────
    def create_folder(self, parent_path: str, folder_name: str) -> bool:
        pb2 = self._pb2()
        r = self._call("CreateFolder",
                       pb2.CreateFolderRequest(parentPath=parent_path, folderName=folder_name))
        return bool(getattr(r, "success", True))

    def copy_file(self, src_paths: list[str], dest_path: str, *,
                  conflict_policy: int = CONFLICT_SKIP) -> dict:
        """上传 = 跨云复制;conflict_policy 默认 Skip(不覆盖目标已有同名)。"""
        pb2 = self._pb2()
        r = self._call("CopyFile", pb2.CopyFileRequest(
            theFilePaths=src_paths, destPath=dest_path, conflictPolicy=conflict_policy))
        return {"success": bool(getattr(r, "success", False)),
                "error": getattr(r, "errorMessage", ""),
                "raw": str(r)[:300]}

    def move_file(self, src_paths: list[str], dest_path: str) -> dict:
        """**移动**(跨云=CD2 内部下载+上传;本地源随之消失 → 上传后无需再删源)。

        与 CopyFile 的区别正是"移走源":用户 2026-09-11 明确要求上传走移动。
        """
        pb2 = self._pb2()
        r = self._call("MoveFile", pb2.MoveFileRequest(
            theFilePaths=src_paths, destPath=dest_path))
        return {"success": bool(getattr(r, "success", False)),
                "error": getattr(r, "errorMessage", "") or "",
                "raw": str(r)[:300]}

    def copy_tasks(self) -> list[dict]:
        """进行中/已完成的复制(上传)任务(CopyTask 无 id,按源/目标路径识别)。"""
        r = self._call("GetCopyTasks", self._empty())
        out = []
        for t in getattr(r, "copyTasks", []) or []:
            total = int(getattr(t, "totalBytes", 0) or 0)
            done = int(getattr(t, "uploadedBytes", 0) or 0)
            out.append({
                "mode": int(getattr(t, "taskMode", 0) or 0),   # 0 Copy / 1 Move
                "status_raw": int(t.status),      # 枚举口径随服务器版本变,勿按名字判
                "source": getattr(t, "sourcePath", "") or "",
                "dest": getattr(t, "destPath", "") or "",
                "total_bytes": total, "uploaded_bytes": done,
                "progress": round(done / total * 100, 1) if total else 0.0,
                "files": int(getattr(t, "totalFiles", 0) or 0),
                "uploaded_files": int(getattr(t, "uploadedFiles", 0) or 0),
                "error": "".join(list(getattr(t, "errors", []) or []))[:200] or "",
            })
        return out

    def delete_file(self, path: str) -> bool:
        pb2 = self._pb2()
        r = self._call("DeleteFile", pb2.FileRequest(path=path, forceRefresh=True))
        return bool(getattr(r, "success", False))
