import sys
import threading
import time


def _load_runtime():
    started = time.monotonic()
    finished = threading.Event()

    def report_progress():
        while not finished.wait(10):
            elapsed = time.monotonic() - started
            print(f"[启动] 仍在加载飞书 SDK 和运行模块，已用 {elapsed:.0f} 秒...", flush=True)

    print("[启动] 正在加载飞书 SDK 和运行模块；加载较慢时会每 10 秒报告进度。", flush=True)
    reporter = threading.Thread(target=report_progress, daemon=True)
    reporter.start()
    try:
        from bot_runtime import runtime
    finally:
        finished.set()
        reporter.join()
    print(f"[启动] 运行模块加载完成，耗时 {time.monotonic() - started:.1f} 秒。", flush=True)
    return runtime


def main():
    runtime = _load_runtime()
    if len(sys.argv) > 1 and sys.argv[1] == "--local-chat":
        runtime.run_local_chat()
    else:
        runtime.run_bot_and_local_chat()


if __name__ == "__main__":
    main()
