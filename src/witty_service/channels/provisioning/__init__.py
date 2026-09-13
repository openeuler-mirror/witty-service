"""扫码接入与手填凭据旁路。

对外入口是 `ProvisioningFlow`（扫码）与 `ManualCredentialBinder`（手填）；两者共用
同一条落库与回滚路径。本模块刻意不导入子模块，避免与 `channels.adapters` 的
导入顺序相互影响。
"""

from __future__ import annotations
