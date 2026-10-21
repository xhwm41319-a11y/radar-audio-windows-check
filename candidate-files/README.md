# RADAR 音频 · Windows x64 通用 CPU 候选

本包面向不同配置的 Windows 电脑，包含应用、Python、FFmpeg、固定模型和离线推理依赖。当前是待 Windows 实机验收的候选，不是已通过全平台验收的正式发行。

1. 解压到可写的本地短路径，例如 `C:\RADAR\Audio`。请勿从压缩包内启动，也不要放入 Program Files、网络盘或超长目录。
2. 打开 `START-HERE.html` 查看配置要求。缺少 Microsoft Visual C++ x64 运行库时，按页面的微软官方链接安装一次。本包不会自动提升权限或修改系统设置。
3. 双击 `Start-RADAR.cmd`，首次创建本目录专用环境并核验依赖与模型，随后打开本机浏览器。保留命令窗口；关闭服务时会清理本次转换子进程。
4. 先用短音频和 3–15 秒参考音色试用，再处理整曲。所有转换在本机进行，模型不联网下载；自动模式在此 CPU 包中使用 CPU。

支持目标：Windows 10 2004及以后/Windows 11，原生 Intel/AMD x64。无独显、AMD/Intel集显、NVIDIA电脑都使用CPU；显卡型号不作为该包安装前提。推荐至少16GiB内存；低于16GiB会提示，未承诺8GiB可完成整曲。解压后安装还需10GiB空闲空间。耗时与音频长度、CPU和内存有关，不能套用Mac实测耗时。

此包不提供CUDA加速依赖。Windows ARM64/32位、旧版Windows、Intel Mac、Android不在本包支持范围。Apple Silicon Mac另用Mac包。

## 检查与恢复

- `Check-RADAR.cmd`：安装/检查依赖后，实际检验本机Windows单实例与任务进程树停止。不运行真实音频，不终止用户的其他程序。该脚本在Windows上通过之前，不可将候选记为Windows验收通过。
- `Retry-Failed-Install.cmd`：仅对本安装器明确记录为failed的环境重试，旧目录保留到`CoverLab/.install-history`；ready、installing或未知环境不覆盖。
- 日志：`CoverLab/runtime/package-install.log`、`native-imports.log`、`doctor-startup.log`。模型任务日志在`CoverLab/CoverLab/data/voice-runs`对应任务目录。不要把私有音频、完整目录或凭据发到公共平台。
- 更新时解压到新目录，旧目录及用户素材保留，不覆盖安装。关闭任务再整理旧目录。

当前完成：专用Windows二进制/依赖组装、离线安装入口、设备选择、单实例锁、Job Object任务停止、依赖/模型校验及可复现验收脚本。
当前尚缺：Windows原生安装、浏览器首用、实际音频生成/取消与跨配置验收。程序提供的分离和声线转换不等于原厂音色还原；听感和相似度需另行试听。


## 不同电脑之间迁移

已安装目录迁移后，每次启动及 Check-RADAR.cmd 都重新执行当前电脑的原生依赖导入与 CPU 运算检查，不沿用原电脑的成功结果；失败保留已有文件，详情见 CoverLab/runtime/native-startup.log。此检查不等于模型整曲、音质或 GPU 已验收。
