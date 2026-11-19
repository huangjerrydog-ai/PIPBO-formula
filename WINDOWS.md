# Windows x64 便携包

## 获取

在GitHub仓库的Actions中打开“Build Windows package”，点击Run workflow。构建成功后，在该次运行页面的Artifacts下载PIPBO-formula-windows-x64。解压外层构件ZIP后，再解压其中的windows-x64.zip。

## 运行

适用于Windows 10/11 x64。不需要安装Python。完整解压PIPBO-formula目录，双击PIPBO-formula.exe；浏览器自动打开本机程序。保留_internal目录，不能只复制exe。关闭控制台窗口或按Ctrl+C停止服务。

数据保存在当前用户的 %LOCALAPPDATA%\PIPBO-formula（数据库及vault.key）。首次运行建立管理员，没有默认密码。升级时替换程序文件夹，数据目录仍保留。备份或迁移必须将pipbo.sqlite3和vault.key一起复制；不要上传数据或密钥到GitHub。可通过环境变量PIPBO_DATA指定数据目录。

如默认端口8775被占用，在程序文件夹终端运行：

```
PIPBO-formula.exe --port 8777
```

浏览器未打开时手动访问http://127.0.0.1:8775/；添加 --no-browser 可禁止自动打开浏览器。服务只监听本机。

## Windows电脑自行构建

安装Python 3.12 x64及Python Launcher，解压源码后双击build_windows.bat。脚本使用独立.venv-build环境安装依赖、运行回归测试，再调用PyInstaller打包并测试实际exe。最终文件在release目录。首次构建需要联网下载依赖。

GitHub构建会自动测试实际exe的页面资源、登录、数据创建、方法管理和重启后数据/密钥持久化。生成SHA256校验文件。本版为便携目录ZIP，未制作安装向导或代码签名。
