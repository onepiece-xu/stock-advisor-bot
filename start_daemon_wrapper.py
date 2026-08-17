"""start_daemon_wrapper.py — 启动 stock-advisor daemon 并重定向日志。

供 StockAdvisor_Daemon.vbs 调用（sh.Run 直启 python，绕开 cmd /c 引号问题）。
daemon 的 logging 输出到 stderr，必须重定向，否则日志丢失。
"""
import subprocess, sys, os

REPO = r"G:\projects\stock-advisor-bot"
log = os.path.join(REPO, "logs", "monitor.log")
err = os.path.join(REPO, "run", "daemon_stderr.log")

def main():
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO
    with open(log, "a") as lo, open(err, "a") as eo:
        proc = subprocess.Popen(
            [r"G:\hermes\venv\Scripts\python.exe", "-B", "-m", "stock_advisor.cli",
             "monitor-daemon", "--config", "config.yaml"],
            cwd=REPO, env=env, stdout=lo, stderr=eo, close_fds=True)
    # 写 PID 文件供 run_status / 外部检测使用（注意：venv python 壳会派生实际解释器，
    # 杀进程时需 taskkill /T 树杀；Windows 无 os.setsid，此处写 Popen 直接返回的 PID）
    try:
        pid_file = os.path.join(REPO, "run", "stock-advisor.pid")
        with open(pid_file, "w", encoding="utf-8") as pf:
            pf.write(str(proc.pid))
    except OSError:
        pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
