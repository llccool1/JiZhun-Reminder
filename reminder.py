# -*- coding: utf-8 -*-
"""
极准定时提醒 v20260930

更新记录（v20260930）:
  [修1] _next_fire/_should_fire 改用锚定数学公式 O(1)，不再从 base 循环累加
  [修2] 数据文件原子写入（tmp + replace）+ .bak 备份，损坏时自动回退
  [修3] winreg/pystray 改为按需/保护导入，非 Windows 也可 import
  [修4] 每 N 分钟/小时/天/周 统一锚定 base 时间触发，消除漂移
  [修5] 重新"启用"：每天/每N分钟/小时/天/周把当前周期标为已处理，等下一个锚定周期；
       已过期的单次提醒保持完成，不突然补发
  [修5b] 新建/改期到过去时间的周期提醒同样从下一个锚定槽位开始，保存瞬间不再立刻响一次
  [修6] 提醒弹窗新增"稍后提醒"（5 / 15 / 30 分钟），向上取整到分钟
  [修7] 日期改日历选择器（tkcalendar，未安装则回退文本框）；时间改时/分 Spinbox
  [修8] 开机自启切换改为状态栏轻提示，不再弹确认框
  [修9] 所有自定义音频统一走独立 MCI 别名（wav 用 waveaudio），试听与提醒互不打断；
       winsound 只做默认提示音/失败回退
  [新] 轻量现代化：ttkbootstrap 主题（flatly/darkly），跟随 Windows 系统深浅色，
       菜单可手动切换；弹窗配色随主题自动适配。未安装 ttkbootstrap 时自动回退
       原生 clam 风格，功能不受影响。

依赖（可选，未安装不影响运行）:
  pip install ttkbootstrap tkcalendar
"""

import json
import os
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import tkinter as tk
from tkinter import messagebox, filedialog

import ctypes

# ---------- 可选依赖：ttkbootstrap（现代化主题） ----------
try:
    import ttkbootstrap as TTK
    HAVE_BOOTSTRAP = True
except ImportError:
    from tkinter import ttk as TTK
    HAVE_BOOTSTRAP = False

# ---------- 可选依赖：tkcalendar（日期选择器） ----------
try:
    from tkcalendar import DateEntry
    HAVE_TKCALENDAR = True
except ImportError:
    HAVE_TKCALENDAR = False

# ---------- 可选依赖：系统托盘 ----------
try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:
    HAVE_TRAY = False

# ---------- 音频 ----------
try:
    import winsound
    HAVE_WIN = True
except Exception:
    HAVE_WIN = False


APP_TITLE   = "极准定时提醒"
APP_VERSION = "v20260930"
AUTOSTART_KEY_NAME = "JiZhunReminder_AutoStart"
PREVIEW_ALIAS = "jizhun_preview"   # 试听专用 MCI 别名，不干扰提醒铃声


def _new_id():
    return uuid.uuid4().hex[:16]


# =========================================================
# 数据 / 日志目录
# =========================================================
def _data_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "JiZhunReminder")
    return os.path.join(os.path.expanduser("~"), ".jizhun_reminder")

DATA_DIR  = _data_dir()
DATA_FILE = os.path.join(DATA_DIR, "reminders.json")
BAK_FILE  = DATA_FILE + ".bak"
LOG_FILE  = os.path.join(DATA_DIR, "error.log")

os.makedirs(DATA_DIR, exist_ok=True)


def log_error(msg, exc=None):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
            if exc is not None:
                fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def _get_winreg():
    """[修3] 按需导入 winreg：非 Windows 平台返回 None，模块可正常 import"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        return winreg
    except ImportError:
        return None


# =========================================================
# 开机自启管理 (Windows Registry)
# =========================================================
def is_autostart_enabled():
    """检查是否已设置开机自启"""
    winreg = _get_winreg()
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ
        )
        val, _ = winreg.QueryValueEx(key, AUTOSTART_KEY_NAME)
        winreg.CloseKey(key)
        return bool(val)
    except Exception:
        return False


def set_autostart(enable=True):
    """设置或取消开机自启"""
    winreg = _get_winreg()
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE
        )
        if enable:
            if getattr(sys, 'frozen', False):
                app_path = sys.executable
            else:
                app_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, AUTOSTART_KEY_NAME, 0, winreg.REG_SZ, app_path)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_KEY_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as exc:
        log_error("set_autostart failed", exc)
        return False


def _system_uses_dark():
    """读取 Windows 系统深浅色设置（AppsUseLightTheme=0 表示深色）"""
    winreg = _get_winreg()
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return val == 0
    except Exception:
        return False


# =========================================================
# 标准北京时间网络同步核心
# =========================================================
TIME_OFFSET = timedelta(0)
IS_TIME_SYNCED = False
LAST_SYNC_SOURCE = "本地时间（未联网）"
LAST_SYNC_TIME_STR = "未同步"


def sync_beijing_time_worker():
    """轮询国内大厂时间源，计算北京时间时间差"""
    global TIME_OFFSET, IS_TIME_SYNCED, LAST_SYNC_SOURCE, LAST_SYNC_TIME_STR

    sources = [
        ("淘宝", "http://api.m.taobao.com/rest/api3.do?api=mtop.common.getTimestamp", "taobao"),
        ("苏宁", "http://quan.suning.com/getSysTime.do", "suning"),
        ("京东", "https://a.jd.com//ajax/queryServerData.html", "jd"),
        ("腾讯", "https://vv.video.qq.com/checktime?otype=json", "tencent"),
    ]

    for name, url, parser_type in sources:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                raw_bytes = resp.read()
                raw_text = raw_bytes.decode("utf-8", errors="ignore")

                net_ts = None
                if parser_type == "taobao":
                    data = json.loads(raw_text)
                    net_ts = int(data["data"]["t"]) / 1000.0
                elif parser_type == "suning":
                    data = json.loads(raw_text)
                    net_dt = datetime.strptime(data["sysTime2"], "%Y-%m-%d %H:%M:%S")
                    net_ts = net_dt.timestamp()
                elif parser_type == "jd":
                    data = json.loads(raw_text)
                    net_ts = int(data["serverTime"]) / 1000.0
                elif parser_type == "tencent":
                    idx = raw_text.find('"t":')
                    if idx != -1:
                        sub = raw_text[idx+4:]
                        end_idx = sub.find(",")
                        net_ts = float(sub[:end_idx])

                if net_ts:
                    beijing_tz = timezone(timedelta(hours=8))
                    beijing_now = datetime.fromtimestamp(net_ts, beijing_tz).replace(tzinfo=None)
                    TIME_OFFSET = beijing_now - datetime.now()
                    IS_TIME_SYNCED = True
                    LAST_SYNC_SOURCE = f"{name}授时源"
                    LAST_SYNC_TIME_STR = datetime.now().strftime("%H:%M:%S")
                    return
        except Exception:
            continue

    if not IS_TIME_SYNCED:
        LAST_SYNC_SOURCE = "本地时间（对时暂未连通）"


def start_time_sync_loop():
    """后台对时服务：启动时立即同步，之后每 2 小时静默自动校准一次"""
    def _loop():
        while True:
            sync_beijing_time_worker()
            time.sleep(7200)
    threading.Thread(target=_loop, daemon=True, name="TimeSyncWorker").start()


def get_now():
    """获取当前标准北京时间"""
    return datetime.now() + TIME_OFFSET


# ============== 常量与字典映射 ==============
REPEAT_TYPES = [
    ("不重复（单次提醒）", "once"),
    ("每 N 分钟",         "minute"),
    ("每 N 小时",         "hour"),
    ("每 N 天",          "day"),
    ("每天",             "daily"),
    ("每周（从开始时间起算）", "weekly"),
]
CODE_TO_LABEL = dict((code, label) for label, code in REPEAT_TYPES)
LABEL_TO_CODE = dict((label, code) for label, code in REPEAT_TYPES)


def format_repeat_label(repeat_code, interval_n):
    n = interval_n or 1
    if repeat_code == "once":
        return "不重复（单次）"
    elif repeat_code == "minute":
        return f"每 {n} 分钟"
    elif repeat_code == "hour":
        return f"每 {n} 小时"
    elif repeat_code == "day":
        return f"每 {n} 天"
    elif repeat_code == "daily":
        return "每天"
    elif repeat_code == "weekly":
        return "每周"
    return CODE_TO_LABEL.get(repeat_code, repeat_code)


POS_TYPES = [
    ("屏幕居中", "center"),
    ("右上角",   "top_right"),
    ("右下角",   "bottom_right"),
    ("左上角",   "top_left"),
    ("左下角",   "bottom_left"),
]
POS_CODE_TO_LABEL = dict((code, label) for label, code in POS_TYPES)
POS_LABEL_TO_CODE = dict((label, code) for label, code in POS_TYPES)

SNOOZE_OPTIONS = (("5 分钟", 5), ("15 分钟", 15), ("30 分钟", 30))


# ============== 主题配色 ==============
def _popup_palette(dark):
    """弹窗 / 卡片配色，随深浅色主题切换"""
    if dark:
        return {
            "bg": "#1e1e1e", "card": "#2b2b2b", "fg": "#e9e9e9",
            "sub": "#9aa0a6", "accent": "#5aa9ff", "accent_dark": "#3f8fe0",
        }
    return {
        "bg": "#F9F9FB", "card": "#FFFFFF", "fg": "#1F2328",
        "sub": "#6E7781", "accent": "#0078D4", "accent_dark": "#005A9E",
    }


# 按钮风格：ttkbootstrap 用 bootstyle，原生 ttk 用自定义 style
if HAVE_BOOTSTRAP:
    KW_BTN_PRIMARY = {"bootstyle": "primary"}
    KW_BTN_OUTLINE = {"bootstyle": "outline-secondary"}
else:
    KW_BTN_PRIMARY = {"style": "Accent.TButton"}
    KW_BTN_OUTLINE = {"style": "Outline.TButton"}

SpinboxCls = TTK.Spinbox if hasattr(TTK, "Spinbox") else tk.Spinbox


# ============== 单实例互斥锁 ==============
_MUTEX_HANDLE = None

def check_single_instance():
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True

    kernel32 = ctypes.windll.kernel32
    mutex_name = "Local\\JiZhunReminder_SingleInstance_Mutex_Lock"

    _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, mutex_name)
    ERROR_ALREADY_EXISTS = 183
    last_error = kernel32.GetLastError()

    if last_error == ERROR_ALREADY_EXISTS:
        MB_OK = 0x00000000
        MB_ICONWARNING = 0x00000030
        MB_TOPMOST = 0x00040000
        ctypes.windll.user32.MessageBoxW(
            0,
            "极准定时提醒程序已经在运行中，请勿重复打开！\n\n如果主界面未显示，请检查桌面右下角系统托盘。",
            "提示",
            MB_OK | MB_ICONWARNING | MB_TOPMOST
        )
        return False
    return True


# ============== 音频播放 ==============
def play_audio(sound_path="", on_finished=None, alias=None):
    """[修9] 所有自定义音频统一走独立 MCI 别名播放；winsound 只做默认提示音/失败回退"""
    def _worker():
        played = False
        my_alias = alias or f"jizhun_{uuid.uuid4().hex[:8]}"
        if sound_path and os.path.exists(sound_path) and sys.platform == "win32":
            try:
                ext = os.path.splitext(sound_path)[1].lower()
                # wav 用 waveaudio，其余用 mpegvideo；各用独立别名，互不打断
                mci_type = "waveaudio" if ext == ".wav" else "mpegvideo"
                winmm = ctypes.windll.winmm
                ret = winmm.mciSendStringW(
                    f'open "{sound_path}" type {mci_type} alias {my_alias}', None, 0, 0)
                if ret == 0:
                    winmm.mciSendStringW(f'play {my_alias} wait', None, 0, 0)
                    winmm.mciSendStringW(f'close {my_alias}', None, 0, 0)
                    played = True
            except Exception as exc:
                log_error("play_audio failed", exc)

        if not played and HAVE_WIN:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

        if on_finished:
            on_finished()

    threading.Thread(target=_worker, daemon=True).start()


def stop_audio(alias=None):
    """停止指定别名的音频；alias=None 时停止全部（仅退出时用）"""
    if sys.platform != "win32":
        return
    try:
        if alias:
            ctypes.windll.winmm.mciSendStringW(f"close {alias}", None, 0, 0)
        else:
            ctypes.windll.winmm.mciSendStringW("close all", None, 0, 0)
            if HAVE_WIN:
                winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass


# ============== 美化弹窗 ==============
def show_stylish_popup(master, title, message, hint="", sound_path="",
                       position="center", on_snooze=None, dark=False):
    """提醒弹窗。[修6] on_snooze 不为 None 时显示"稍后提醒"按钮组。"""
    play_audio(sound_path)

    try:
        pal = _popup_palette(dark)
        w = tk.Toplevel(master)
        w.title(title)
        w.attributes("-topmost", True)
        w.configure(bg=pal["bg"])
        w.resizable(False, False)

        win_w, win_h = 420, (268 if on_snooze else 220)
        screen_w = w.winfo_screenwidth()
        screen_h = w.winfo_screenheight()

        margin = 24
        if position == "top_left":
            x, y = margin, margin
        elif position == "top_right":
            x, y = max(0, screen_w - win_w - margin), margin
        elif position == "bottom_left":
            x, y = margin, max(0, screen_h - win_h - margin - 48)
        elif position == "bottom_right":
            x, y = max(0, screen_w - win_w - margin), max(0, screen_h - win_h - margin - 48)
        else:
            x, y = max(0, (screen_w - win_w) // 2), max(0, (screen_h - win_h) // 2)

        w.geometry(f"{win_w}x{win_h}+{x}+{y}")

        color_bar = tk.Frame(w, bg=pal["accent"], height=5)
        color_bar.pack(fill="x", side="top")

        body = tk.Frame(w, bg=pal["card"], padx=20, pady=16)
        body.pack(fill="both", expand=True)

        header = tk.Frame(body, bg=pal["card"])
        header.pack(fill="x", pady=(0, 8))

        lbl_icon = tk.Label(header, text="⏰", font=("Segoe UI Emoji", 14),
                            bg=pal["card"], fg=pal["accent"])
        lbl_icon.pack(side="left", padx=(0, 6))

        lbl_title = tk.Label(header, text=title, font=("Microsoft YaHei UI", 12, "bold"),
                             bg=pal["card"], fg=pal["fg"])
        lbl_title.pack(side="left")

        lbl_msg = tk.Label(
            body, text=message, font=("Microsoft YaHei UI", 11),
            bg=pal["card"], fg=pal["fg"], justify="left", wraplength=370
        )
        lbl_msg.pack(fill="x", expand=True, anchor="w", pady=(0, 4))

        def on_close():
            w.destroy()

        if on_snooze is not None:
            snooze_bar = tk.Frame(body, bg=pal["card"])
            snooze_bar.pack(fill="x", pady=(10, 0))
            tk.Label(snooze_bar, text="稍后提醒：", font=("Microsoft YaHei UI", 9),
                     bg=pal["card"], fg=pal["sub"]).pack(side="left")
            for label, mins in SNOOZE_OPTIONS:
                b = tk.Button(
                    snooze_bar, text=label, font=("Microsoft YaHei UI", 9),
                    bg=pal["card"], fg=pal["accent"],
                    activebackground=pal["card"], activeforeground=pal["accent_dark"],
                    relief="solid", bd=1, padx=8, pady=2, cursor="hand2",
                    highlightbackground=pal["accent"],
                    command=lambda m=mins: (on_snooze(m), on_close()),
                )
                b.pack(side="left", padx=(4, 0))

        bottom_bar = tk.Frame(body, bg=pal["card"])
        bottom_bar.pack(fill="x", side="bottom", pady=(8, 0))

        if hint:
            lbl_hint = tk.Label(bottom_bar, text=hint, font=("Microsoft YaHei UI", 9),
                                bg=pal["card"], fg=pal["sub"])
            lbl_hint.pack(side="left", anchor="w")

        btn_ok = tk.Button(
            bottom_bar, text="我知道了", font=("Microsoft YaHei UI", 9, "bold"),
            bg=pal["accent"], fg="#FFFFFF",
            activebackground=pal["accent_dark"], activeforeground="#FFFFFF",
            relief="flat", bd=0, padx=14, pady=4, cursor="hand2",
            command=on_close
        )
        btn_ok.pack(side="right")
        btn_ok.bind("<Enter>", lambda _e: btn_ok.config(bg=pal["accent_dark"]))
        btn_ok.bind("<Leave>", lambda _e: btn_ok.config(bg=pal["accent"]))

        w.bind("<Return>", lambda _e: on_close())
        w.bind("<Escape>", lambda _e: on_close())
        w.after(60000, lambda: w.destroy() if w.winfo_exists() else None)
        w.focus_force()

    except Exception as exc:
        log_error("show_stylish_popup failed", exc)


# ============== 提醒序列化（可独立测试） ==============
def _reminder_to_dict(r):
    return {
        "id":          r["id"],
        "content":     r["content"],
        "date":        r["date"].strftime("%Y-%m-%d"),
        "time":        r["time"].strftime("%Y-%m-%d %H:%M"),
        "repeat":      r["repeat"],
        "interval_n":  r["interval_n"],
        "sound":       r.get("sound", ""),
        "position":    r.get("position", "center"),
        "disabled":    r["disabled"],
        "created":     r["created"].strftime("%Y-%m-%d %H:%M:%S"),
        "last_fire":   r["last_fire"].strftime("%Y-%m-%d %H:%M:%S") if r.get("last_fire") else None,
    }


def _reminder_from_dict(it):
    """解析失败时抛异常，由调用方决定跳过该条"""
    return {
        "id":         it["id"],
        "content":    it["content"],
        "date":       datetime.strptime(it["date"], "%Y-%m-%d"),
        "time":       datetime.strptime(it["time"], "%Y-%m-%d %H:%M"),
        "repeat":     it["repeat"],
        "interval_n": it.get("interval_n", 0) or 0,
        "sound":      it.get("sound", ""),
        "position":   it.get("position", "center"),
        "disabled":   bool(it.get("disabled", False)),
        "created":    datetime.strptime(it["created"], "%Y-%m-%d %H:%M:%S"),
        "last_fire":  datetime.strptime(it["last_fire"], "%Y-%m-%d %H:%M:%S") if it.get("last_fire") else None,
    }


# ============== 提醒对话框 ==============
def _snooze_fire_at(minutes, now=None):
    """[修6] 稍后提醒触发时刻：先按分钟向上取整，再加 minutes，保证至少等待所选时长"""
    now = now or get_now()
    base = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes + 1)
    return base


def _can_reenable(r, now=None):
    """[修5] 重新启用是否有效：已过期的单次提醒不再启用，避免突然补发历史提醒"""
    now = now or get_now()
    if r["repeat"] == "once" and r["time"] <= now:
        return False
    return True


class ReminderDialog(tk.Toplevel):

    def __init__(self, master, on_save, reminder=None):
        super().__init__(master)
        self.reminder   = reminder
        self.on_save    = on_save
        self.is_edit    = reminder is not None
        self.interval_n = tk.StringVar(value=str(reminder.get("interval_n", 60)) if reminder and reminder.get("interval_n") else "60")
        self.sound_var  = tk.StringVar(value=reminder.get("sound", "") if reminder else "")
        self.is_previewing = False

        self.title("编辑提醒" if self.is_edit else "新建提醒")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        sub_kw = {"bootstyle": "secondary"} if HAVE_BOOTSTRAP else {"foreground": "#666"}

        frm = TTK.Frame(self, padding=20)
        frm.grid(sticky="nsew")

        TTK.Label(frm, text="提醒内容：").grid(row=0, column=0, sticky="e", pady=(0, 8))
        self.content_var = tk.StringVar(value=reminder.get("content", "") if reminder else "")
        content_in = TTK.Entry(frm, textvariable=self.content_var, width=42)
        content_in.grid(row=0, column=1, columnspan=2, sticky="we", pady=(0, 12))

        # ---- [修7] 时间：时/分 Spinbox ----
        TTK.Label(frm, text="触发时间：").grid(row=1, column=0, sticky="e", pady=6)
        if reminder:
            init_h, init_m = reminder["time"].hour, reminder["time"].minute
        else:
            _t = get_now() + timedelta(minutes=5)
            init_h, init_m = _t.hour, _t.minute
        self.hour_var = tk.StringVar(value=str(init_h))
        self.min_var  = tk.StringVar(value=str(init_m))
        time_box = TTK.Frame(frm)
        time_box.grid(row=1, column=1, sticky="w", pady=6)
        SpinboxCls(time_box, from_=0, to=23, wrap=True, width=4,
                   textvariable=self.hour_var).pack(side="left")
        TTK.Label(time_box, text=" : ").pack(side="left")
        SpinboxCls(time_box, from_=0, to=59, wrap=True, width=4,
                   textvariable=self.min_var).pack(side="left")
        TTK.Label(frm, text="（北京时间，24 小时制）", **sub_kw).grid(
            row=1, column=2, sticky="w", padx=(6, 0))
        # ---- [修7] 日期：日历选择器（无 tkcalendar 则回退文本框） ----
        TTK.Label(frm, text="触发日期：").grid(row=2, column=0, sticky="e", pady=6)
        if HAVE_TKCALENDAR:
            self.date_entry = DateEntry(frm, date_pattern="yyyy-mm-dd",
                                        width=13, state="readonly")
            if reminder:
                self.date_entry.set_date(reminder["date"].date())
            else:
                self.date_entry.set_date(get_now().date())
            self.date_entry.grid(row=2, column=1, sticky="w", pady=6)
            self.date_var = None
        else:
            self.date_entry = None
            self.date_var = tk.StringVar()
            if reminder:
                self.date_var.set(reminder["date"].strftime("%Y-%m-%d"))
            else:
                self.date_var.set(get_now().strftime("%Y-%m-%d"))
            TTK.Entry(frm, textvariable=self.date_var, width=20).grid(
                row=2, column=1, sticky="w", pady=6)
        TTK.Label(frm, text="（点击右侧日历图标选择日期）" if HAVE_TKCALENDAR
                  else "（格式 年-月-日，如 2026-09-28）", **sub_kw).grid(
            row=2, column=2, sticky="w", padx=(6, 0))

        TTK.Label(frm, text="重复方式：").grid(row=3, column=0, sticky="e", pady=6)
        initial_code = reminder["repeat"] if reminder else "once"
        self.repeat_var = tk.StringVar(value=CODE_TO_LABEL.get(initial_code, REPEAT_TYPES[0][0]))
        self.cb = TTK.Combobox(frm, textvariable=self.repeat_var, width=28, state="readonly")
        self.cb['values'] = [label for label, _ in REPEAT_TYPES]
        self.cb.grid(row=3, column=1, columnspan=2, sticky="w", pady=6)
        self.cb.bind("<<ComboboxSelected>>", self._on_repeat_change)

        self.n_frame = TTK.Frame(frm)
        self.n_frame.grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 4))
        TTK.Label(self.n_frame, text="间隔（N）：").grid(row=0, column=0, sticky="e", padx=(0, 6))
        TTK.Entry(self.n_frame, textvariable=self.interval_n, width=8).grid(row=0, column=1)

        self._on_repeat_change()

        TTK.Label(frm, text="弹窗位置：").grid(row=5, column=0, sticky="e", pady=6)
        initial_pos = reminder.get("position", "center") if reminder else "center"
        self.pos_var = tk.StringVar(value=POS_CODE_TO_LABEL.get(initial_pos, POS_TYPES[0][0]))
        self.pos_cb = TTK.Combobox(frm, textvariable=self.pos_var, width=28, state="readonly")
        self.pos_cb['values'] = [label for label, _ in POS_TYPES]
        self.pos_cb.grid(row=5, column=1, columnspan=2, sticky="w", pady=6)
        TTK.Label(frm, text="提醒铃声：").grid(row=6, column=0, sticky="e", pady=6)
        snd_box = TTK.Frame(frm)
        snd_box.grid(row=6, column=1, columnspan=2, sticky="we", pady=6)
        self.snd_entry = TTK.Entry(snd_box, textvariable=self.sound_var, width=24)
        self.snd_entry.pack(side="left", fill="x", expand=True)
        TTK.Button(snd_box, text="浏览…", width=6, command=self._choose_sound,
                   **KW_BTN_OUTLINE).pack(side="left", padx=(4, 0))
        self.preview_btn = TTK.Button(snd_box, text="试听", width=6,
                                      command=self._toggle_preview, **KW_BTN_OUTLINE)
        self.preview_btn.pack(side="left", padx=(4, 0))
        TTK.Button(snd_box, text="默认", width=5, command=self._clear_sound,
                   **KW_BTN_OUTLINE).pack(side="left", padx=(4, 0))
        TTK.Label(
            frm, justify="left", **sub_kw,
            text=(
                "说明：\n"
                "  · 本软件所有提醒均精确对齐[标准北京时间]触发\n"
                "  · 单次提醒 = 到点后自动完成，不再触发\n"
                "  · 每 N 分钟 / 每 N 小时 / 每 N 天 = 到点按间隔重复\n"
                "  · 铃声留空则使用系统默认提示音"
            ),
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(14, 0))

        btns = TTK.Frame(frm)
        btns.grid(row=8, column=0, columnspan=3, sticky="e", pady=(20, 0))
        TTK.Button(btns, text="取消", width=9, command=self._on_cancel,
                   **KW_BTN_OUTLINE).pack(side="right", padx=(8, 0))
        TTK.Button(btns, text="保存", width=9, command=self.save,
                   **KW_BTN_PRIMARY).pack(side="right")

        self.update_idletasks()
        self.geometry(
            f"+{master.winfo_rootx() + master.winfo_width() // 2 - self.winfo_width() // 2}"
            f"+{master.winfo_rooty() + master.winfo_height() // 2 - self.winfo_height() // 2}"
        )
        content_in.focus_set()

    def _choose_sound(self):
        f = filedialog.askopenfilename(
            parent=self,
            title="选择提醒音频",
            filetypes=[("音频文件", "*.mp3 *.wav *.wma"), ("所有文件", "*.*")]
        )
        if f:
            self.sound_var.set(os.path.normpath(f))

    def _clear_sound(self):
        self.sound_var.set("")
        self._stop_preview()

    def _stop_preview(self):
        # [修9] 试听与提醒铃声各用独立 MCI 别名，只停试听自己的，不影响提醒
        stop_audio(PREVIEW_ALIAS)
        self.is_previewing = False
        try:
            self.preview_btn.config(text="试听")
        except Exception:
            pass

    def _toggle_preview(self):
        if self.is_previewing:
            self._stop_preview()
        else:
            snd = self.sound_var.get().strip()
            self.is_previewing = True
            self.preview_btn.config(text="停止")
            play_audio(snd, alias=PREVIEW_ALIAS,
                       on_finished=lambda: self.after(0, self._stop_preview))

    def _on_cancel(self):
        self._stop_preview()
        self.destroy()

    def _on_repeat_change(self, event=None):
        code = LABEL_TO_CODE.get(self.repeat_var.get(), "once")
        if code in ("minute", "hour", "day"):
            self.n_frame.grid()
        else:
            self.n_frame.grid_remove()

    def save(self):
        try:
            content = self.content_var.get().strip()
            if not content:
                messagebox.showwarning("提示", "请填写提醒内容", parent=self)
                return

            try:
                hour, minute = int(self.hour_var.get()), int(self.min_var.get())
                assert 0 <= hour <= 23 and 0 <= minute <= 59
            except Exception:
                messagebox.showerror("时间错误", "小时应在 0-23，分钟应在 0-59 之间", parent=self)
                return

            if HAVE_TKCALENDAR:
                d = self.date_entry.get_date()
                date_dt = datetime(d.year, d.month, d.day)
            else:
                d_raw = self.date_var.get().strip()
                try:
                    y_s, m_s, d_s = d_raw.split("-")
                    year, month, day = int(y_s), int(m_s), int(d_s)
                    date_dt = datetime(year, month, day)
                except Exception:
                    messagebox.showerror("日期格式错误", '日期格式应为 YYYY-MM-DD，例如 "2026-09-28"', parent=self)
                    return

            repeat_code = LABEL_TO_CODE.get(self.repeat_var.get(), "once")
            interval_n = 0
            if repeat_code in ("minute", "hour", "day"):
                try:
                    interval_n = int(self.interval_n.get())
                    assert interval_n > 0
                except Exception:
                    messagebox.showerror("间隔错误", "请填写大于 0 的整数间隔", parent=self)
                    return

            snd_path = self.sound_var.get().strip()
            if snd_path and not os.path.exists(snd_path):
                if not messagebox.askyesno("提示", "所选音频文件路径不存在，是否依然保存？", parent=self):
                    return

            pos_code = POS_LABEL_TO_CODE.get(self.pos_var.get(), "center")
            target_time = datetime(date_dt.year, date_dt.month, date_dt.day, hour, minute)

            last_fire = None
            if self.is_edit:
                if self.reminder["time"] != target_time or repeat_code == "once" or self.reminder["repeat"] != repeat_code:
                    last_fire = None
                else:
                    last_fire = self.reminder.get("last_fire")

            # [修] 新建/改期到过去时间的周期提醒：把当前周期视为已处理，从下一个锚定槽位
            #      开始，避免保存瞬间立刻响一次（与"重新启用"语义一致；once 的过去时间
            #      仍按"过期即提醒一次"处理；今天还没到点的 daily 保持 None 到点触发）
            if repeat_code != "once" and last_fire is None and target_time <= get_now():
                last_fire = get_now()

            reminder = {
                "id":         self.reminder["id"] if self.is_edit else _new_id(),
                "content":    content,
                "date":       date_dt,
                "time":       target_time,
                "repeat":     repeat_code,
                "interval_n": interval_n,
                "sound":      snd_path,
                "position":   pos_code,
                "disabled":   False,
                "created":    self.reminder["created"] if self.is_edit else get_now(),
                "last_fire":  last_fire,
            }

            self._stop_preview()
            self.on_save(reminder)
            self.destroy()
        except Exception as exc:
            log_error("ReminderDialog.save failed", exc)
            messagebox.showerror("保存失败", f"保存提醒时出错：\n{exc}")


# ============== 主窗口 ==============
_BaseTk = TTK.Window if HAVE_BOOTSTRAP else tk.Tk


class ReminderApp(_BaseTk):

    def __init__(self):
        self._dark_mode = _system_uses_dark() if HAVE_BOOTSTRAP else False
        if HAVE_BOOTSTRAP:
            super().__init__(themename="darkly" if self._dark_mode else "flatly")
        else:
            super().__init__()

        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.resizable(True, True)

        self.geometry("1140x420")
        self.minsize(1060, 360)

        self._load_app_icon()

        self.reminders = {}
        self.manager   = ReminderManager(self)
        self.tray_icon = None
        self._flash_text = ""
        self._flash_until = 0

        start_time_sync_loop()

        self._setup_modern_style()
        self._build_menu()
        self._build_ui()
        self.load()
        self.refresh()
        self._center_window()

        self._init_pystray()
        self._start_clock()

    def _load_app_icon(self):
        ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
        if os.path.exists(ico_path):
            try:
                self.iconbitmap(ico_path)
            except Exception:
                pass

    def _setup_modern_style(self):
        if HAVE_BOOTSTRAP:
            # ttkbootstrap 主题已接管基础样式，只需定义卡片配色
            self._apply_card_style()
            return

        style = TTK.Style(self)
        style.theme_use("clam")

        style.configure(".", background="#F3F3F3", font=("Microsoft YaHei UI", 9))
        style.configure("TFrame", background="#F3F3F3")
        style.configure("Card.TFrame", background="#FFFFFF", relief="flat")

        style.configure("Notice.TLabel", font=("Microsoft YaHei UI", 9, "bold"), background="#FFFFFF", foreground="#0969DA")
        style.configure("Clock.TLabel", font=("Consolas", 10), background="#FFFFFF", foreground="#24292F")
        style.configure("LocalClock.TLabel", font=("Consolas", 9), background="#FFFFFF", foreground="#656D76")
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 9), background="#F3F3F3", foreground="#656D76")

        style.configure(
            "Accent.TButton",
            font=("Microsoft YaHei UI", 9, "bold"),
            background="#0078D4",
            foreground="#FFFFFF",
            borderwidth=0,
            focuscolor="none",
            padding=(14, 5)
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#106EBE"), ("pressed", "#005A9E"), ("disabled", "#D1D5DB")],
            foreground=[("disabled", "#9CA3AF")]
        )

        style.configure(
            "Outline.TButton",
            font=("Microsoft YaHei UI", 9),
            background="#FFFFFF",
            foreground="#24292F",
            borderwidth=1,
            relief="solid",
            focuscolor="none",
            padding=(10, 4)
        )
        style.map(
            "Outline.TButton",
            background=[("active", "#F6F8FA"), ("pressed", "#EAEEF2")],
            foreground=[("disabled", "#8C959F")]
        )

        style.configure(
            "Modern.Treeview",
            background="#FFFFFF",
            foreground="#24292F",
            fieldbackground="#FFFFFF",
            borderwidth=0,
            rowheight=32,
            font=("Microsoft YaHei UI", 9)
        )
        style.configure(
            "Modern.Treeview.Heading",
            background="#F6F8FA",
            foreground="#57606A",
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(6, 6)
        )
        style.map(
            "Modern.Treeview",
            background=[("selected", "#E8F2FE")],
            foreground=[("selected", "#0969DA")]
        )
        style.map(
            "Modern.Treeview.Heading",
            background=[("active", "#ECEFF2")]
        )

    def _apply_card_style(self):
        """ttkbootstrap 下的卡片配色，随深浅色主题刷新"""
        if not HAVE_BOOTSTRAP:
            return
        pal = _popup_palette(self._dark_mode)
        try:
            self.style.configure("Card.TFrame", background=pal["card"])
            self.style.configure("Card.TLabel", background=pal["card"],
                                 foreground=pal["fg"], font=("Microsoft YaHei UI", 9))
            self.style.configure("CardTitle.TLabel", background=pal["card"],
                                 foreground=pal["accent"],
                                 font=("Microsoft YaHei UI", 15, "bold"))
            self.style.configure("Hint.TLabel", background=pal["card"],
                                 foreground=pal["accent"],
                                 font=("Microsoft YaHei UI", 9))
        except Exception as exc:
            log_error("apply card style failed", exc)

    def _toggle_dark(self):
        self._dark_mode = bool(self.dark_var.get())
        try:
            self.style.theme_use("darkly" if self._dark_mode else "flatly")
        except Exception as exc:
            log_error("toggle dark theme failed", exc)
        self._apply_card_style()
        # [修] 状态圆点 Canvas 背景跟随主题刷新
        try:
            if getattr(self, "_dot_canvas", None):
                self._dot_canvas.config(bg=_popup_palette(self._dark_mode)["card"])
        except Exception as exc:
            log_error("dot canvas bg update failed", exc)
        self.refresh()
        self.flash_status("已切换为深色模式" if self._dark_mode else "已切换为浅色模式")

    def _create_tray_icon_image(self):
        ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
        if os.path.exists(ico_path):
            try:
                return Image.open(ico_path)
            except Exception:
                pass

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill="#0078D4", outline="#ffffff", width=4)
        d.line((32, 32, 32, 16), fill="#ffffff", width=5)
        d.line((32, 32, 46, 32), fill="#ffffff", width=5)
        return img

    def _init_pystray(self):
        if not HAVE_TRAY:
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", lambda: self.show_from_tray(), default=True),
            pystray.MenuItem("彻底退出程序", lambda: self.real_exit()),
        )
        self.tray_icon = pystray.Icon(
            "ReminderApp",
            self._create_tray_icon_image(),
            APP_TITLE,
            menu
        )
        self.tray_icon.run_detached()

    def _start_clock(self):
        self.refresh()
        self.after(1000, self._start_clock)

    def _build_menu(self):
        menu = tk.Menu(self)
        f = tk.Menu(menu, tearoff=0)
        f.add_command(label="新建提醒", command=self.new_dialog)
        f.add_separator()
        f.add_command(label="标记选中为已完成", command=lambda: self.set_done(True))
        f.add_command(label="启用选中",         command=lambda: self.set_done(False))
        f.add_command(label="删除选中",         command=self.delete_selected)
        f.add_separator()

        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        f.add_checkbutton(
            label="开机自启动",
            variable=self.autostart_var,
            command=self._toggle_autostart
        )

        if HAVE_BOOTSTRAP:
            self.dark_var = tk.BooleanVar(value=self._dark_mode)
            f.add_checkbutton(
                label="深色模式",
                variable=self.dark_var,
                command=self._toggle_dark
            )

        f.add_separator()
        f.add_command(label="最小化到托盘", command=self.minimize_to_tray)
        f.add_command(label="彻底退出程序", command=self.real_exit)
        menu.add_cascade(label="提醒", menu=f)

        m = tk.Menu(menu, tearoff=0)
        m.add_command(label="打开数据目录", command=self.open_data_dir)
        m.add_command(label="打开错误日志", command=self.open_log)
        m.add_separator()
        m.add_command(label="关于", command=lambda: messagebox.showinfo(
            "关于",
            f"{APP_TITLE} {APP_VERSION}\n\n"
            "· 标准北京时间多路网络授时校准\n"
            "· 倒计时精确至秒，拒绝时间漂移\n"
            "· 支持开机自启与后台托盘守护\n"
            "· 支持稍后提醒、深色模式"
        ))
        menu.add_cascade(label="文件", menu=m)
        self.config(menu=menu)

    def _toggle_autostart(self):
        # [修8] 成功时只在状态栏轻提示，不再弹确认框
        target_state = self.autostart_var.get()
        success = set_autostart(target_state)
        if success:
            self.flash_status("已启用开机自启动" if target_state else "已关闭开机自启动")
        else:
            self.autostart_var.set(not target_state)
            messagebox.showerror("自启动设置失败", "无法修改注册表自启动项，请检查权限。", parent=self)

    def flash_status(self, msg, seconds=3):
        """[修8] 状态栏临时轻提示"""
        self._flash_text = msg
        self._flash_until = time.time() + seconds

    def _build_ui(self):
        pal = _popup_palette(self._dark_mode)
        card_bg = pal["card"] if HAVE_BOOTSTRAP else "#FFFFFF"

        container = TTK.Frame(self, padding=(16, 10, 16, 10))
        container.pack(fill="both", expand=True)

        top_card = TTK.Frame(container, style="Card.TFrame", padding=(16, 8))
        top_card.pack(fill="x", pady=(0, 8))

        left_box = TTK.Frame(top_card, style="Card.TFrame")
        left_box.pack(side="left")

        indicator = tk.Canvas(left_box, width=12, height=12,
                              bg=card_bg, highlightthickness=0)
        indicator.pack(side="left", padx=(0, 8))
        indicator.create_oval(2, 2, 10, 10, fill="#2DA44E", outline="")
        self._dot_canvas = indicator  # [修] 主题切换时刷新背景

        if HAVE_BOOTSTRAP:
            TTK.Label(left_box, text=APP_TITLE,
                      style="CardTitle.TLabel").pack(side="left")
            TTK.Label(left_box, text="（本提醒按标准北京时间触发）",
                      style="Hint.TLabel").pack(side="left", padx=(10, 0))
        else:
            tk.Label(left_box, text=APP_TITLE, font=("Microsoft YaHei UI", 15, "bold"),
                     bg="#FFFFFF", fg="#005FB8").pack(side="left")
            TTK.Label(left_box, text="（本提醒按标准北京时间触发）",
                      style="Notice.TLabel").pack(side="left", padx=(10, 0))

        right_box = TTK.Frame(top_card, style="Card.TFrame")
        right_box.pack(side="right")

        clock_style = "Card.TLabel" if HAVE_BOOTSTRAP else "Clock.TLabel"
        local_style = "Card.TLabel" if HAVE_BOOTSTRAP else "LocalClock.TLabel"
        self.bj_clock_lbl = TTK.Label(right_box, text="标准北京时间：--",
                                      style=clock_style, font=("Consolas", 10))
        self.bj_clock_lbl.pack(anchor="e")
        self.local_clock_lbl = TTK.Label(right_box, text="本地电脑时间：--",
                                         style=local_style, font=("Consolas", 9))
        self.local_clock_lbl.pack(anchor="e")

        # 操作栏
        bar = TTK.Frame(container)
        bar.pack(fill="x", pady=(0, 8))

        TTK.Button(bar, text="＋ 新建提醒", command=self.new_dialog,
                   **KW_BTN_PRIMARY).pack(side="left")
        TTK.Button(bar, text="编辑", command=self.edit_selected,
                   **KW_BTN_OUTLINE).pack(side="left", padx=(8, 0))
        TTK.Button(bar, text="删除", command=self.delete_selected,
                   **KW_BTN_OUTLINE).pack(side="left", padx=(6, 0))
        TTK.Button(bar, text="完成", command=lambda: self.set_done(True),
                   **KW_BTN_OUTLINE).pack(side="left", padx=(6, 0))
        TTK.Button(bar, text="启用", command=lambda: self.set_done(False),
                   **KW_BTN_OUTLINE).pack(side="left", padx=(6, 0))

        TTK.Button(bar, text="测试通知", command=self.test_notification,
                   **KW_BTN_OUTLINE).pack(side="right")

        table_card = TTK.Frame(container, style="Card.TFrame", padding=1)
        table_card.pack(fill="both", expand=True)

        cols = ("content", "schedule", "repeat", "sound", "position", "next_fire", "status")
        tree_kw = {} if HAVE_BOOTSTRAP else {"style": "Modern.Treeview"}
        self.tree = TTK.Treeview(table_card, columns=cols, show="headings", **tree_kw)
        self.tree.heading("content",    text="  提醒内容")
        self.tree.heading("schedule",   text="计划时间(北京)")
        self.tree.heading("repeat",     text="重复方式")
        self.tree.heading("sound",      text="自定义铃声")
        self.tree.heading("position",   text="弹窗位置")
        self.tree.heading("next_fire",  text="下次触发(北京)")
        self.tree.heading("status",     text="倒计时状态")

        self.tree.column("content",   width=240, minwidth=140, stretch=True, anchor="w")
        self.tree.column("schedule",  width=140, stretch=False, anchor="center")
        self.tree.column("repeat",    width=135, stretch=False, anchor="center")
        self.tree.column("sound",     width=110, stretch=False, anchor="center")
        self.tree.column("position",  width=95,  stretch=False, anchor="center")
        self.tree.column("next_fire", width=145, stretch=False, anchor="center")
        self.tree.column("status",    width=165, stretch=False, anchor="center")

        self.tree.tag_configure("disabled", foreground="#8C959F")
        if not HAVE_BOOTSTRAP:
            self.tree.tag_configure("normal", foreground="#24292F")

        vsb = TTK.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda _e: self.edit_selected())
        self.tree.bind("<Return>",   lambda _e: self.edit_selected())
        self.tree.bind("<Delete>",   lambda _e: self.delete_selected())

        bottom_box = TTK.Frame(container)
        bottom_box.pack(fill="x", pady=(6, 0))
        status_style = {} if HAVE_BOOTSTRAP else {"style": "Status.TLabel"}
        self.status = TTK.Label(bottom_box, text="加载中…", **status_style)
        self.status.pack(side="left")

        self.sync_status_lbl = TTK.Label(bottom_box,
                                         text=f"网络校准：{LAST_SYNC_SOURCE}",
                                         **status_style)
        self.sync_status_lbl.pack(side="right")

    def _center_window(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")

    # ---------- 托盘交互与唤醒 ----------
    def minimize_to_tray(self):
        # [修] 无托盘环境只做普通最小化，避免窗口彻底消失后无法恢复
        if HAVE_TRAY and self.tray_icon:
            self.withdraw()
        else:
            self.iconify()

    def show_from_tray(self):
        def _restore():
            self.deiconify()
            self.state('normal')
            try:
                hwnd = self.winfo_id()
                user32 = ctypes.windll.user32
                user32.keybd_event(0x12, 0, 0, 0)
                user32.keybd_event(0x12, 0, 2, 0)
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            self.lift()
            self.attributes("-topmost", True)
            self.after(50, lambda: self.attributes("-topmost", False))
            self.focus_force()

        self.after(0, _restore)

    def real_exit(self):
        def _exit():
            if messagebox.askyesno("退出提醒", "确定彻底退出极准定时提醒？\n退出后将无法接收到点通知。"):
                stop_audio()
                if self.tray_icon:
                    try:
                        self.tray_icon.visible = False
                        self.tray_icon.stop()
                    except Exception:
                        pass
                self.manager.stop()
                self.save()
                self.destroy()
                os._exit(0)

        self.after(0, _exit)

    # ---------- 数据 ----------
    def _load_file(self):
        # [修2] 主文件损坏时自动回退 .bak
        for path in (DATA_FILE, BAK_FILE):
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except Exception as exc:
                log_error(f"load {path} failed", exc)
                continue

            items = []
            for it in raw:
                try:
                    items.append(_reminder_from_dict(it))
                except Exception as exc:
                    log_error("parse reminder item failed", exc)
                    continue
            if path == BAK_FILE:
                log_error("主数据文件损坏，已从 .bak 恢复")
            return items
        return []

    def load(self):
        self.reminders = {r["id"]: r for r in self._load_file()}
        self.manager.update_reminders(self.reminders)
        self.manager.start()

    def save(self):
        # [修2] 原子写入：先写 tmp 再 replace；旧文件保留为 .bak
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp_path = DATA_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump([_reminder_to_dict(r) for r in self.reminders.values()],
                          fh, ensure_ascii=False, indent=2)
            if os.path.exists(DATA_FILE):
                try:
                    os.replace(DATA_FILE, BAK_FILE)
                except OSError as exc:
                    log_error("backup reminders.json failed", exc)
            os.replace(tmp_path, DATA_FILE)
        except Exception as exc:
            log_error("save reminders.json failed", exc)
            return False
        return True

    # ---------- 列表与倒计时 ----------
    def refresh(self):
        try:
            selected = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            total = len(self.reminders)
            bj_now = get_now()
            local_now = datetime.now()

            for r in sorted(self.reminders.values(),
                            key=lambda x: (self.manager._next_fire(x) or datetime.max)):
                n   = self.manager._next_fire(r)
                rep = format_repeat_label(r["repeat"], r.get("interval_n"))
                nxt = n.strftime("%Y-%m-%d %H:%M") if n else "—"
                snd_name = os.path.basename(r.get("sound", "")) if r.get("sound") else "默认"
                pos_name = POS_CODE_TO_LABEL.get(r.get("position", "center"), "屏幕居中")
                if r["disabled"]:
                    status = "已完成"
                    tag = "disabled"
                elif n:
                    status = self._humanize(n - bj_now)
                    tag = "normal"
                else:
                    status = "—"
                    tag = "disabled"

                self.tree.insert("", "end", iid=r["id"], values=(
                    f"  {r['content']}",
                    f'{r["date"].strftime("%Y-%m-%d")} {r["time"].strftime("%H:%M")}',
                    rep,
                    snd_name,
                    pos_name,
                    nxt,
                    status,
                ), tags=(tag,))

            valid_selection = [item_id for item_id in selected if item_id in self.reminders]
            if valid_selection:
                self.tree.selection_set(valid_selection)

            active = sum(1 for r in self.reminders.values() if not r["disabled"])
            # [修8] 轻提示优先显示，超时后恢复常规状态
            if time.time() < self._flash_until:
                self.status.config(text=self._flash_text)
            else:
                self.status.config(text=f"共 {total} 条提醒  ·  生效中 {active} 条")

            self.bj_clock_lbl.config(
                text=f"标准北京时间：{bj_now.strftime('%Y-%m-%d %H:%M:%S')}")
            self.local_clock_lbl.config(
                text=f"本地电脑时间：{local_now.strftime('%Y-%m-%d %H:%M:%S')}")
            self.sync_status_lbl.config(
                text=f"网络校准：{LAST_SYNC_SOURCE} (上次同步: {LAST_SYNC_TIME_STR})")

        except Exception as exc:
            log_error("refresh failed", exc)

    @staticmethod
    def _humanize(delta: timedelta) -> str:
        s = int(delta.total_seconds())
        if s < 0:   return "逾期"
        if s == 0:  return "即将触发"

        d, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        m, sec = divmod(r, 60)

        parts = []
        if d:
            parts.append(f"{d}天")
        if h or d:
            parts.append(f"{h:02d}小时" if d else f"{h}小时")
        if m or h or d:
            parts.append(f"{m:02d}分" if (h or d) else f"{m}分")
        parts.append(f"{sec:02d}秒" if (m or h or d) else f"{sec}秒")

        return "".join(parts)

    # ---------- 操作 ----------
    def new_dialog(self):
        ReminderDialog(self, on_save=self._on_save)

    def _on_save(self, reminder):
        self.reminders[reminder["id"]] = reminder
        self.save()
        self.manager.update_reminders(self.reminders)
        self.refresh()

    def edit_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择一条提醒（可双击）")
            return
        ReminderDialog(self, on_save=self._on_save, reminder=self.reminders[sel[0]])

    def set_done(self, done):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要操作的提醒")
            return
        now = get_now()
        skipped = 0
        for rid in sel:
            r = self.reminders.get(rid)
            if r is None:
                continue
            if not done and not _can_reenable(r, now):
                # [修5] 已过期的单次提醒：保持完成，不突然补发
                skipped += 1
                continue
            r["disabled"] = done
            if not done:
                # [修5] 重新启用：把当前周期标记为已处理，等待下一个锚定周期，不立刻补发
                r["last_fire"] = None
                if r["repeat"] == "daily":
                    today_target = datetime.combine(now.date(), r["time"].time())
                    if now >= today_target:
                        r["last_fire"] = now
                    # 今天时点未到：保持 None，到点正常触发
                elif r["repeat"] in ("minute", "hour", "day", "weekly"):
                    r["last_fire"] = now
                # once：_can_reenable 已保证 base 在未来，保持 None 等待到点触发
        self.save()
        self.manager.update_reminders(self.reminders)
        self.refresh()
        if skipped:
            messagebox.showinfo("提示", f"{skipped} 条单次提醒的时间已过，保持完成状态，未重新启用。")

    def delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要删除的提醒")
            return
        if not messagebox.askyesno("确认删除", f"确认删除选中的 {len(sel)} 条提醒？此操作不可撤销。"):
            return
        for rid in sel:
            self.reminders.pop(rid, None)
        self.save()
        self.manager.update_reminders(self.reminders)
        self.refresh()

    def snooze_reminder(self, reminder, minutes):
        """[修6] 稍后提醒：生成一条一次性提醒，N 分钟后触发"""
        fire_at = _snooze_fire_at(minutes)
        new = {
            "id":         _new_id(),
            "content":    reminder["content"],
            "date":       datetime(fire_at.year, fire_at.month, fire_at.day),
            "time":       fire_at,
            "repeat":     "once",
            "interval_n": 0,
            "sound":      reminder.get("sound", ""),
            "position":   reminder.get("position", "center"),
            "disabled":   False,
            "created":    get_now(),
            "last_fire":  None,
        }
        self._on_save(new)
        self.flash_status(f"已设置 {minutes} 分钟后再次提醒")

    def test_notification(self):
        show_stylish_popup(
            self,
            APP_TITLE,
            "通知功能正常。本软件所有提醒均精确对齐标准北京时间触发。",
            hint=f"北京时间：{get_now().strftime('%Y-%m-%d %H:%M:%S')}",
            position="center",
            dark=self._dark_mode,
        )

    def open_data_dir(self):
        if sys.platform == "win32":
            os.startfile(DATA_DIR)

    def open_log(self):
        if not os.path.exists(LOG_FILE):
            messagebox.showinfo("日志", "目前没有错误日志。")
            return
        if sys.platform == "win32":
            os.startfile(LOG_FILE)

    def on_fire(self, reminder):
        rep = format_repeat_label(reminder["repeat"], reminder.get("interval_n"))
        show_stylish_popup(
            self,
            APP_TITLE,
            reminder["content"],
            hint=f'北京时间 {reminder["time"].strftime("%H:%M")}  ·  {rep}',
            sound_path=reminder.get("sound", ""),
            position=reminder.get("position", "center"),
            on_snooze=lambda mins, rem=reminder: self.snooze_reminder(rem, mins),
            dark=self._dark_mode,
        )

    def on_tick(self):
        self.refresh()


# ============== 后台提醒管理器 ==============
class ReminderManager:
    def __init__(self, window):
        self.window    = window
        self.reminders = {}
        self.lock      = threading.Lock()
        self._stop     = threading.Event()
        self._thread   = None

    def update_reminders(self, reminders):
        with self.lock:
            self.reminders = reminders

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ReminderLoop")
        self._thread.start()

    def stop(self):
        self._stop.set()

    @staticmethod
    def _step_for(r):
        """[修1/修4] 各重复类型的固定步长；触发时刻永远锚定 base，不漂移"""
        code = r["repeat"]
        iv = max(1, r.get("interval_n") or 1)
        if code == "minute":
            return timedelta(minutes=iv)
        if code == "hour":
            return timedelta(hours=iv)
        if code == "day":
            return timedelta(days=iv)
        if code == "weekly":
            return timedelta(weeks=1)
        return None

    def _next_fire(self, r):
        """[修1] 数学公式 O(1) 计算下次触发时刻，严格锚定用户设定的时分"""
        if r["disabled"]:
            return None
        now  = get_now()
        base = r["time"]
        code = r["repeat"]

        # 1. 单次提醒
        if code == "once":
            if r.get("last_fire") or base <= now:
                return None
            return base

        # 2. 每天（时分永远固定为 base.time()；起始日期在未来则等到那一天）
        if code == "daily":
            if now < base:
                return base
            target = datetime.combine(now.date(), base.time())
            prev = r.get("last_fire")
            if target > now and not (prev and prev.date() == now.date()):
                return target
            return target + timedelta(days=1)

        # 3. 每 N 分钟 / 小时 / 天 / 周：锚定 base 的等差数列
        step = self._step_for(r)
        if step is None:
            return None
        if now < base:
            return base
        k = int((now - base).total_seconds() // step.total_seconds()) + 1
        return base + k * step

    def _should_fire(self, r):
        """[修1/修4] 判断当前是否应该触发：取锚定槽位，与上次触发槽位比较"""
        if r["disabled"]:
            return False
        now  = get_now()
        base = r["time"]
        if now < base:
            return False
        prev = r.get("last_fire")
        code = r["repeat"]

        if code == "once":
            return prev is None

        if code == "daily":
            if prev and prev.date() == now.date():
                return False
            return now >= datetime.combine(now.date(), base.time())

        step = self._step_for(r)
        if step is None:
            return False
        step_s = step.total_seconds()
        # 当前所在的锚定槽位序号
        k = int((now - base).total_seconds() // step_s)
        if prev is None or prev < base:
            return True
        pk = int((prev - base).total_seconds() // step_s)
        return k > pk

    def _run(self):
        while not self._stop.is_set():
            try:
                to_fire = []
                with self.lock:
                    snapshot = list(self.reminders.values())
                for r in snapshot:
                    try:
                        if self._should_fire(r):
                            to_fire.append(r)
                    except Exception as exc:
                        log_error("should_fire failed", exc)
                        continue

                if to_fire:
                    now = get_now()
                    for r in to_fire:
                        self.window.after(0, lambda rem=r: self.window.on_fire(rem))
                        r["last_fire"] = now
                        if r["repeat"] == "once":
                            r["disabled"] = True

                    # [修4] 任何触发都要落盘 last_fire，否则重启后会重复触发已处理周期
                    self.window.after(0, self.window.save)

                    self.window.after(0, self.window.on_tick)

                self._stop.wait(1)
            except Exception as exc:
                log_error("ReminderManager loop error", exc)
                self._stop.wait(2)


# ============== 入口 ==============
def main():
    if not check_single_instance():
        sys.exit(0)

    try:
        app = ReminderApp()
        app.protocol("WM_DELETE_WINDOW", app.minimize_to_tray)
        app.mainloop()
    except Exception as exc:
        log_error("main() unhandled exception", exc)


if __name__ == "__main__":
    main()
