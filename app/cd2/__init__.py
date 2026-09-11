"""CloudDrive2 gRPC 客户端(上传段)。

- `clouddrive.proto` 为官方发布(https://www.clouddrive2.com/api/clouddrive.proto,v1.0.14);
  `clouddrive_pb2*.py` 用下面这条命令生成:

      cd app/cd2 && python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. clouddrive.proto

- 生成后需把 `clouddrive_pb2_grpc.py` 里的 `import clouddrive_pb2` 改为
  `from . import clouddrive_pb2`:生成器按顶层模块名导入,包内不可用。
- 认证:API 令牌 → 元数据头 `Authorization: Bearer <token>`
  (用户名密码走 `GetToken` 换 JWT;`GetSystemInfo`/`GetApiTokenInfo` 免认证)。
"""
