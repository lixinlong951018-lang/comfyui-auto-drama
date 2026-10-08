# Windows：启动与关闭短剧控制台

双击项目根目录的 **启动短剧控制台.bat** 或 **关闭短剧控制台.bat**。桌面同名快捷方式执行相同入口。仅管理 Auto Drama 的 8890 控制台和 chain_daemon，ComfyUI 8188 始终独立。

## 使用

- 启动：已有健康组件继续使用，缺失组件补齐；快速重复点击只保留一个控制台和一个 daemon，5 秒内只打开一次浏览器。
- 关闭：扫描本项目所有服务进程，包括绝对路径、工作目录内相对路径、后台旧实例和已注册包装启动；不要求 PID 文件或原启动窗口存在。
- 关闭网页不会停止服务。关闭整个调度器须使用关闭按钮；已提交的 ComfyUI 任务继续运行，重启后原状态继续通过已有监控逻辑核对。
- 无运行实例时仍提示“短剧控制台已关闭”。失败弹窗说明原因；8890 若被其他进程占用，只报告 PID，不结束该进程。
- 需要本机 Python 3.10+（Windows 64 位 Python）；标准库实现，无额外服务或进程管理依赖，无 UAC 提权。
- 控制台日志：console/batch_console.log、batch_console.err.log；守护日志：console/chain_daemon.log、chain_daemon.err.log。

命令行也复用同一机制：

```powershell
python console/start_daemons.py
python console/windows_control.py start --no-browser
python console/windows_control.py stop --no-browser
python console/windows_control.py status
```

直接执行 batch_console.py 或 chain_daemon.py 时，组件入口的项目级 Windows 命名互斥锁仍阻止重复实例。旧 scripts/local_console.ps1 保留为兼容入口，委托相同管理器。

## 身份与退出边界

- Windows 命名互斥锁分别保护启停操作和组件生命周期；进程退出自动释放，无 PID/锁文件假状态。
- 通过 Windows 进程命令行、远程工作目录和脚本规范路径识别旧实例；新实例另有按项目、服务、PID、创建时间命名的停止事件，可识别包装启动。
- 正常关闭向全部确认属于项目的服务发送停止事件。控制台停止接收新连接并等待已有请求线程退出；daemon 完成本轮已有状态保存后退出。
- 最多等待 30 秒，超时仅对再次确认脚本/原生服务身份及创建时间的目标强制结束，防止 PID 复用误杀。旧版服务没有正常停止事件，可能进入该回退。
- 最后再次扫描项目服务和 8890 监听；仍有进程/端口会明确报错。只删除 console/.windows-runtime.json（浏览器去重时间），不清理数据库、素材、视频或工作流。
- 不调用 ComfyUI interrupt/free/queue-delete；不结束 ComfyUI 或无关 Python。**强制回退无法替旧版服务补救尚未保存的内存状态；已提交并持久化的记录及 SQLite 事务保留。**

## 2026-10-08 本机验收

| 项目 | 结果 |
|---|---|
| 1. ComfyUI 未启动，Auto Drama 独立启停 | 通过 |
| 2. ComfyUI 已启动，Auto Drama 启停不影响 8188 | 通过 |
| 3. 8 次并发启动，组件各 1 个 | 通过；直接重复组件启动也被拒绝 |
| 4. 启动入口退出后，关闭仍找到后台服务 | 通过 |
| 5. 相对路径手动启动、start_daemons.py、包装启动 | 通过 |
| 6. 2 个旧版控制台（8890/8891）+ 2 个旧版 daemon | 全部清理，未依赖 PID 文件 |
| 7. 并发重复关闭 | 通过 |
| 8. 关闭后 8890 无监听，daemon 无残留 | 通过 |
| 9. 重启后 SQLite 状态完整恢复 | 前后 state 全记录一致，integrity_check=ok |
| 10. H3 生成运行中关闭 Auto Drama | 同一 ComfyUI 任务继续运行并成功；重启恢复完成记录，无重复提交 |
| 11. 无关 Python、无关 8890 程序 | 均未终止；占用端口时报告而不误杀 |

另测五次并发浏览器开启请求：仪表化浏览器回调只调用一次。没有自动化点击真实浏览器窗口或弹窗；快捷方式参数与底层入口已核对。测试结果和 ComfyUI history 只保留本地 local-validation/lifecycle，不上传个人素材、数据库或历史图。

本次未改 H3 参数、I2VA/FL2VA 工作流、extract_last_frame 或链式生成业务逻辑，仅新增 Windows 生命周期钩子。
