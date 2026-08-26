@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在生成图标...
python make_icon.py
echo 正在打包成独立程序，请稍等（大约一两分钟）...
python -m PyInstaller --noconfirm --clean --windowed --onefile --name MachinePulse --icon icon.ico --add-data "index.html;." --add-data "icon.ico;." --hidden-import server --hidden-import pystray._win32 --collect-all pystray app.py
if exist "dist\MachinePulse.exe" (
  copy /Y "dist\MachinePulse.exe" "电脑健康看板.exe" >nul
  echo.
  echo 完成：%~dp0电脑健康看板.exe
) else (
  echo 打包失败，请查看上面的报错。
)
pause
