# JiZhun-Reminder
# 极准定时提醒 (Windows Reminder)
一款轻量、极简且高颜值的 Windows 桌面定时提醒小工具(可自定义铃声)。自带国内大厂网络授时校准，彻底告别因电脑本地时间误差导致的漏提醒或时间漂移。

## ✨ 核心特性
- ⏱️ **高精度网络授时**：多路轮询阿里、苏宁、京东、腾讯等国内授时源，自动对齐标准北京时间。
- ⏳ **秒级动态倒计时**：精确到“天/时/分/秒”实时递减，提醒时刻严格锚定。
- 🔔 **个性化提醒**：支持自定义 5 个屏幕弹窗方位（屏幕居中、四角），支持导入 `.mp3 / .wav` 自定义铃声。
- 🔄 **多样化周期**：支持单次、每 N 分钟、每 N 小时、每 N 天、每天、每周等循环。
- 🛡️ **现代系统适配**：深度适配 Win10/Win11 原生 XAML 系统托盘架构（点 X 隐藏、退出干净无残留）；原生 Mutex 单实例防多开。
- 🚀 **开机自启动**：基于注册表 Run 项一键勾选开关，无需管理员提权。
<img width="1142" height="472" alt="image" src="https://github.com/user-attachments/assets/4f20271a-7805-4b83-a09f-06470e468e0f" />
<img width="1142" height="472" alt="image" src="https://github.com/user-attachments/assets/482fff75-bc20-45ae-9159-7898f6e7d50e" />
<img width="1142" height="472" alt="image" src="https://github.com/user-attachments/assets/68daff35-f60b-4b40-b551-b1e3ca41dd63" />

## 📦 打包编译
```bash
pip install -r requirements.txt pyinstaller
pyinstaller --clean -F -w -i app.ico --add-data "app.ico;." reminder.py -n "极准定时提醒"
