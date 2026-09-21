# Windows 弹出与 SLEEP 停转实验方案

本实验不使用 STANDBY、START STOP UNIT 回退。2026-09-20 已在本机 E 盘完成一轮真实弹出和人工停转验收；其他硬件组合及正式 GUI 集成仍需验证。
现有源码和 dist 程序保持不动，使用独立诊断程序排除旧通知补丁和 SMART 扫描的干扰。

## 设计依据与边界

1. Windows 移除成功与硬盘电机停转是两个独立结果。IOCTL 成功只说明命令被接受；不等于测到了转速。
2. ATA SLEEP (E6) 后不再用 TUR、SMART、文件读取验证，不自动重新在线、复位或重试。
3. 程序的 sleeping_disks 集合只约束自身监控，不是 Windows 的全局 I/O 黑名单。磁盘离线可隔离文件系统访问，但不能阻止其他管理员程序或驱动直接访问物理设备。
4. 不能把 Win32 error 5 当作“ReFS 不支持锁卷”并强制卸载。锁卷失败即停止，不发送 SLEEP。
5. Windows 的 QUERYREMOVE 是询问阶段，仍可能被其他程序否决。不能假设此时所有卷都已落盘，或可以安全深睡。
6. DEVNODES_CHANGED 不是某块盘的弹出通知；TUR 失败不能证明正在弹出。REMOVECOMPLETE 后发命令也没有可靠送达窗口。

## 主流程：应用发起的受控弹出

用户选定磁盘 → 校验盘符、物理盘序号、桥接序列号、USB 总线 → 枚举全部卷（包括无盘符的卷）与实际弹出桥节点 → 确认桥只对应目标盘 → 停止本程序对磁盘的访问并等待现有 I/O 完成。

实验阶段要求先从托盘退出旧 GUI，保留仅在关机阶段执行停转的服务。正式集成应使用每盘事务门禁，将扫描、自动休眠、手动唤醒和弹出统一串行化，而不是从 cached_disks 删除条目来假装暂停。

随后：

1. 打开物理盘句柄，再次通过该句柄核对序列号和设备序号。
2. 对全部卷取得 FSCTL_LOCK_VOLUME；任一失败立即释放已有锁并退出。
3. 刷文件系统缓存、卸载已锁定卷。
4. 设置磁盘 OFFLINE（Persist=False），读取磁盘属性确认离线。
5. 使用预先打开的物理盘句柄发 ATA FLUSH CACHE (E7)，必须得到成功响应。
6. 关闭全部卷句柄。
7. 先写实验日志，再只发送一次 ATA SLEEP (E6)。记录 Win32 错误、SCSI 状态和 sense。
8. 关闭物理盘句柄，然后仅向确认的 USB 存储桥发一次 CM_Request_Device_EjectW。
9. 记录 CONFIGRET、veto 类型/对象和耗时。禁止回退移除 Hub 或强制移除子树。
10. Windows 弹出结果由 PnP 返回值确认；物理停转由人工听音/触感或独立功耗测量确认。

这是需要实测的工程假设：先离线能减少 E6 后的文件系统 I/O，使 PnP 收尾有机会完成。PnP/桥固件仍可能需要磁盘响应，因此不能预先保证所有桥接组合都成功。

## 失败与恢复

| 阶段 | 处理 |
| --- | --- |
| 身份校验、卷枚举、锁卷失败 | 不发停转命令，不强制卸载 |
| 离线或 E7 失败，尚未尝试 E6 | 尽力恢复在线，关闭句柄；恢复失败单独报错 |
| E6 返回失败或超时 | 命令可能已送达，保留离线，状态为“停转未知”，不重试 |
| E6 接受，PnP 被否决 | 保留离线，明确“未安全弹出”，记录 veto；不能提示可直接拔盘 |
| PnP 请求未返回 | 日志停留在 pnp_request，不伪装成后台请求已取消，不并发重试 |
| PnP 成功 | 标记“Windows 已弹出、SLEEP 已接受、物理停转待观察” |

E6 后需要恢复访问时，应作为独立的用户操作，先核对设备身份和事务状态，再安排目标设备的重新上电/复位及恢复在线；实验工具不自动执行。

## Windows 托盘入口

纯用户态程序不能保证在任意 Windows 原生托盘弹出中都获得一个“卷已经彻底卸载、但磁盘仍接受 E6”的安全窗口。

可交付的第一阶段是程序自己的“停转并安全弹出”入口，或程序托盘菜单的同名入口，均调用上述事务。Windows 原生弹出事件仅做释放句柄和状态维护，不能依赖事后补发 SLEEP。

如果必须让 Windows 原生安全删除硬件按钮也保证物理停转，需要另行评估与目标桥固件配合，或实现并签名存储过滤驱动，在正确的 PnP/电源请求阶段协调 I/O 和停转。这不是目前 Python 通知补丁已经具备的“驱动级”能力，也不能用模拟测试替代驱动/硬件验收。

## 测试入口

- `tools/test_eject_sleep_protocol.py`：只用假设备测试顺序、占用失败、回滚、E6 结果不确定和 PnP veto，不访问硬盘。
- `tools/eject_sleep_probe.py --drive E --output <新日志路径>`：默认仅枚举，绝不修改硬盘状态。
- 实测额外指定 `--execute --expect-serial B000BBBBAAAA`，需要管理员权限和旧 GUI 已退出。日志必须位于其他硬盘。
- 本机批准目标：E: / PhysicalDrive4 / USB ASMT ASMT105x / 桥序列号 B000BBBBAAAA / ReFS；执行时仍重新核对，不依赖固定磁盘序号。

## 验收标准

必须同时满足：目标盘身份正确、全部卷成功隔离、E7/E6 无报错、测试句柄已全部释放、PnP 成功、人工确认停转、其他盘保持在线。记录完成后保持无 I/O 观察至少 60 秒。重新连接后的文件系统和关键文件核验作为单独步骤，不能在确认停转前读盘。

## 官方资料

- [FSCTL_LOCK_VOLUME：有打开的文件就会失败，成功锁卷前会刷缓存](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_lock_volume)
- [IOCTL_VOLUME_OFFLINE：卸载后阻止卷重新挂载](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-ioctl_volume_offline)
- [离线卷不会阻止直接物理盘 I/O](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddvol/ni-ntddvol-ioctl_volume_offline)
- [Microsoft WSL 的锁卷、卸载和磁盘离线实现](https://github.com/microsoft/WSL/blob/master/src/windows/common/disk.cpp)
- [处理设备移除请求：关闭句柄，移除失败时恢复通知](https://learn.microsoft.com/zh-cn/windows/win32/devio/processing-a-request-to-remove-a-device)

## 本轮结果：2026-09-20

已通过 9 个模拟测试方法（含 20 个成功/故障场景），未把模拟结果当作硬件结果。只读预检确认 E: 为 ReFS、USB、非启动盘/系统盘，分页文件位于 C:。用户从托盘退出旧 GUI 后，由独立管理员进程执行。

硬件：16 TB，ASMT ASMT105x / UASPStor，USB VID_174C&PID_55AA，桥实例尾号 MSFT30AAAABBBB000B，桥序列号 B000BBBBAAAA，目标卷 GUID 187d5a39-4976-40b1-a67d-7e72d4504f13。

| 本地时间（UTC+8） | 实测结果 |
| --- | --- |
| 21:39:38.415 | 身份、完整卷清单和专用 USB 存储桥检查通过 |
| 21:39:40.051 | ReFS 卷锁定成功；“ReFS 恒不支持锁卷”在本机不成立 |
| 21:39:40.055 | 卷卸载成功 |
| 21:39:40.056 | 磁盘离线成功并读回确认 |
| 21:39:40.059 | E7 成功，SCSI status=0 |
| 21:39:42.049 | E6 成功，约 2 秒，SCSI status=0 |
| 21:39:43.056 | CM_Request_Device_EjectW 返回 CR_SUCCESS=0，veto=0，约 1 秒 |
| 21:40:40.102 | PnP 后查：目标盘已不在 PresentOnly 清单；G 盘及三块内置盘状态 OK |
| 用户观察 | 用户明确回复“已停转，持续 60 秒未重新转动” |

从身份校验到弹出约 4.64 秒，未使用 STANDBY、未强制卸载、未通过禁用杀毒/索引或强制删除设备子树实现。

证据：工作区 `tools/eject_E_trial1_20260920.jsonl`（逐阶段落盘日志）、`tools/eject_E_postcheck_20260920.json`（PnP/事件查询）。这些本机日志不作为公共发布内容。

结论：本硬件组合已有一轮“Windows 安全弹出 + 物理停转”的成功实测。没有测试原生 Windows 托盘在旧补丁开启时的行为，也没有完成反复热插拔、多卷并发、取消流程、系统关机和重新连接后的文件校验。因此先保留独立工具，不直接替换正式程序或宣称全机型通过。

## 正式集成清单

- 在进程间阻止重复管理实例；每盘采用稳定身份绑定，不能只用可复用的 PhysicalDrive 序号。
- 后台枚举/SMART、手动操作、自动休眠和设备通知共享每盘事务状态，开始弹出后拒绝新 I/O，并等待已有 I/O 结束。
- 保存原磁盘身份再暂停访问，不删除身份后再查 serial；按盘记录 SLEEP 结果，不提前显示休眠。
- 取消常驻 RW 句柄补丁的全盘 TUR 探测、移除后全盘补发 SLEEP；系统通知负责及时释放句柄和维护状态。
- 应用按钮与应用托盘的“停转并安全弹出”共用本事务；不再先调用失效的 ShellExecute Eject，也不先深睡再尝试锁卷。
- E6 后任何不确定结果保留隔离状态，GUI 单独显示恢复入口。后台不能自动把盘重新在线。
- PnP 请求未返回时显示“仍在等待 Windows”，保留事务所有权，禁止重复弹出；线程等待超时不等于取消 Windows 请求。
- 本轮只验证弹出事务，不把结论直接推广到系统关机阶段。
