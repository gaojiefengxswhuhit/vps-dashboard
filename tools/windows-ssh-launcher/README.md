# Windows 一键 SSH 登录

这个目录给仪表盘的一键登录按钮提供本机支持。

浏览器不能直接执行本机 PowerShell 命令，所以页面会打开 `vpsssh://` 自定义协议，再由本机脚本校验参数并启动 SSH。

## 安装

在这台 Windows 电脑上打开 PowerShell，进入本目录后运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\Install-VpsSshProtocol.ps1
```

安装只写入当前用户的注册表：

```text
HKCU\Software\Classes\vpsssh
```

不需要管理员权限。

## 使用

打开仪表盘，点击 VPS 卡片里的“一键登录”。

浏览器第一次会提示是否允许打开外部应用，选择允许后会弹出 PowerShell 窗口并执行对应 SSH 登录。

## 卸载

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\Uninstall-VpsSshProtocol.ps1
```

## 安全说明

本地处理脚本只接受 `vpsssh://connect` 传入的 `user`、`host`、`port`、`key` 参数，并且会校验字段格式，然后只执行 `ssh`。它不会执行网页传来的任意 PowerShell 命令。

脚本还会限制目标主机必须存在于 `https://nezha.xushuo.uk/status/vps-status.json`，并内置当前 VPS 的 IP 作为离线兜底白名单。
