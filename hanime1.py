from __future__ import annotations

import atexit
import gc
import inspect
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

from cloakbrowser import launch_persistent_context


# ============================================================================
# 配置
# ============================================================================
HEADLESS = False
TARGET_URL = "https://hanime1.com/"

NDM_HOST = "127.0.0.1"
NDM_PORT = 10007
NDM_EXTENSION_ID = "pbghcbaeehloijjcebiflemhcebmlnke"

# 时间参数（秒）
INITIAL_WAIT = 15              # 页面打开后等待播放器/扩展就绪
PANEL_RETRY = 6                # 检测浮窗的最大重试次数
PANEL_RETRY_INTERVAL = 2       # 每次重试间隔
POST_CLICK_WAIT = 5            # 点击后等待 NDM 处理

CHROMIUM_ARGS = [
    "--test-type",
    "--disable-infobars",
    "--no-default-browser-check",
    "--no-first-run",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--disable-features=Translate,TranslateUI,AcceptCHFrame,"
    "MediaRouter,OptimizationHints,ChromeWhatsNewUI",
    "--lang=zh-CN",
    "--disable-component-update",
]


# ============================================================================
# 临时目录管理（退出即焚）
# ============================================================================
_cleanup_dirs: list[Path] = []


def _atexit_cleanup() -> None:
    for p in list(_cleanup_dirs):
        try:
            robust_rmtree(p, retries=6, delay=0.3, verbose=False)
        except Exception:
            pass
    _cleanup_dirs.clear()


atexit.register(_atexit_cleanup)


def make_temp_dir(prefix: str = "ndm_") -> Path:
    p = Path(tempfile.mkdtemp(prefix=prefix))
    _cleanup_dirs.append(p)
    return p


def robust_rmtree(path: Path, retries: int = 15, delay: float = 0.5,
                  verbose: bool = True) -> bool:
    """Windows 下删除临时目录的健壮版：retry + chmod + gc。"""
    path = Path(path)
    if not path.exists():
        return True

    def _on_error(func, p, exc_info):
        try:
            os.chmod(p, 0o777)
            func(p)
        except Exception:
            pass

    for i in range(retries):
        try:
            shutil.rmtree(path, onerror=_on_error)
        except Exception:
            pass
        if not path.exists():
            if verbose and i > 0:
                print(f"  第 {i+1} 次重试成功")
            return True
        gc.collect()
        time.sleep(delay)

    shutil.rmtree(path, ignore_errors=True)
    ok = not path.exists()
    if verbose and not ok:
        print(f"  [WARN] 无法完全删除：{path}")
    return ok


def cleanup_all(verbose: bool = True) -> None:
    for p in list(_cleanup_dirs):
        ok = robust_rmtree(p, verbose=verbose)
        if verbose:
            print(f"  {'OK  ' if ok else 'FAIL'}  {p}")
    _cleanup_dirs.clear()


# ============================================================================
# 扩展：从本机已安装的 NDM 扩展复制
# ============================================================================
def find_installed_ndm_extension() -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return None

    bases = []
    for browser in ("Microsoft/Edge", "Google/Chrome"):
        for profile in ("Default", "Profile 1", "Profile 2", "Profile 3"):
            bases.append(Path(local) / browser / "User Data" / profile
                         / "Extensions" / NDM_EXTENSION_ID)

    found: list[Path] = []
    for base in bases:
        if not base.exists():
            continue
        for version_dir in base.iterdir():
            if version_dir.is_dir() and (version_dir / "manifest.json").exists():
                found.append(version_dir)

    if not found:
        return None

    def vkey(p: Path):
        try:
            return tuple(int(x) for x in p.name.split("."))
        except ValueError:
            return (0,)

    found.sort(key=vkey, reverse=True)
    return found[0]


def prepare_extension(target: Path) -> None:
    installed = find_installed_ndm_extension()
    if not installed:
        raise RuntimeError(
            "未找到本机已安装的 NDM 扩展。\n"
            "请先在 Edge（或 Chrome）里安装 NeatDownloadManager Extension，"
            "然后重新运行本脚本。"
        )
    print(f"[EXT] 找到 NDM 扩展：{installed}")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(installed, target, dirs_exist_ok=True)
    print(f"[EXT] 已复制到临时目录：{target}")


# ============================================================================
# 工具
# ============================================================================
def tcp_listening(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ============================================================================
# 页面 JS：检测浮窗 / 派发 mousedown
# ============================================================================
NDM_PANEL_PROBE_JS = r"""
() => {
  const panels = [...document.querySelectorAll('[id^="neatDiv"]')];
  return {
    count: panels.length,
    panels: panels.map(p => {
      const rect = p.getBoundingClientRect();
      const rows = [...p.querySelectorAll('table tr')];
      return {
        id: p.id,
        visible: p.style.display !== 'none' && rect.width > 0 && rect.height > 0,
        styleDisplay: p.style.display,
        rowCount: rows.length,
        rows: rows.slice(0, 8).map(r => ({
          text: (r.textContent || '').trim().slice(0, 150),
          cellCount: r.querySelectorAll('td').length,
        })),
      };
    }),
  };
}
"""

NDM_PANEL_CLICK_JS = r"""
() => {
  const panels = [...document.querySelectorAll('[id^="neatDiv"]')];
  if (panels.length === 0) return {ok: false, reason: 'no panel'};
  const panel = panels[0];
  const rows = [...panel.querySelectorAll('table tr')];
  if (rows.length < 2) return {ok: false, reason: 'no data rows (only ' + rows.length + ')'};

  const results = [];
  // 第 0 行是标题行，数据从第 1 行开始
  for (let i = 1; i < rows.length; i++) {
    try {
      const ev = new MouseEvent('mousedown', {
        bubbles: true, cancelable: true, view: window,
        button: 0, buttons: 1,
      });
      rows[i].dispatchEvent(ev);
      results.push('row ' + i + ' mousedown');
    } catch (e) {
      results.push('row ' + i + ' error: ' + e.message);
    }
  }
  return {ok: true, results, panelId: panel.id, rowCount: rows.length};
}
"""


# ============================================================================
# 流程
# ============================================================================
def wait_for_panel(page, max_attempts: int = PANEL_RETRY,
                   interval: float = PANEL_RETRY_INTERVAL) -> dict | None:
    for i in range(1, max_attempts + 1):
        try:
            info = page.evaluate(NDM_PANEL_PROBE_JS)
        except Exception as e:
            print(f"  [{i}/{max_attempts}] 探测失败：{e}")
            time.sleep(interval)
            continue

        count = info.get("count", 0)
        if count > 0:
            visible = [p for p in info["panels"] if p.get("visible")]
            if visible:
                p = visible[0]
                print(f"  [{i}/{max_attempts}] 发现可见浮窗：{p['id']}  行数={p['rowCount']}")
                return info
            print(f"  [{i}/{max_attempts}] 有 {count} 个浮窗但都不可见")
        else:
            print(f"  [{i}/{max_attempts}] 暂无浮窗")
        time.sleep(interval)
    return None


def get_live_page(context):
    pages = list(context.pages)
    if not pages:
        return context.new_page()
    normals = [p for p in pages if not p.url.startswith("chrome-extension://")]
    return normals[-1] if normals else pages[-1]


def main() -> int:
    # ---- 1) 临时工作区 ----
    profile_dir = make_temp_dir("ndm_profile_")
    ext_dir = make_temp_dir("ndm_ext_")
    print("=" * 78)
    print("NDM 自动下载器")
    print("=" * 78)
    print(f"[WORKSPACE] profile : {profile_dir}")
    print(f"[WORKSPACE] ext     : {ext_dir}")

    try:
        prepare_extension(ext_dir)
    except RuntimeError as e:
        print(f"[FATAL] {e}")
        return 1

    context = None
    try:
        # ---- 2) 检查 NDM 桌面端 ----
        bridge = tcp_listening(NDM_HOST, NDM_PORT)
        print(f"[NDM] TCP {NDM_HOST}:{NDM_PORT} → "
              f"{'LISTENING' if bridge else 'NOT LISTENING'}")
        if not bridge:
            print("[WARN] NDM 桌面端没有在监听，请先启动 NDM。")

        # ---- 3) 启动浏览器并加载扩展 ----
        sig = inspect.signature(launch_persistent_context)
        effective_args = list(CHROMIUM_ARGS) + [
            f"--disable-extensions-except={ext_dir}",
            f"--load-extension={ext_dir}",
        ]
        launch_kwargs = {"headless": HEADLESS, "accept_downloads": True}
        for key in ("args", "chromium_args", "extra_args", "browser_args", "launch_args"):
            if key in sig.parameters:
                launch_kwargs[key] = effective_args
                break
        if "extension_paths" in sig.parameters:
            launch_kwargs["extension_paths"] = [str(ext_dir)]

        print("[LAUNCH] 启动 Chromium...")
        context = launch_persistent_context(str(profile_dir), **launch_kwargs)

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.bring_to_front()
        except Exception:
            pass

        # ---- 4) 打开目标页面 ----
        print(f"[NAV] 打开 {TARGET_URL}")
        resp = page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        if resp:
            print(f"[NAV] HTTP {resp.status}")

        print(f"[WAIT] 等待 {INITIAL_WAIT}s 让播放器/扩展就绪...")
        time.sleep(INITIAL_WAIT)

        page = get_live_page(context)

        # ---- 5) 检测 NDM 浮窗 ----
        print("[PANEL] 检测 NDM 浮窗...")
        info = wait_for_panel(page)
        if not info:
            print("[PANEL] 未检测到 NDM 浮窗。")
            print("        可能原因：视频未加载 / 扩展未捕获到媒体 / 页面结构变化。")
            print("        按回车键关闭。")
            input()
            return 2

        # 展示浮窗内容
        for p in info.get("panels", []):
            if p.get("visible"):
                for r in p.get("rows", []):
                    print(f"        {r.get('text')}")
                break

        # ---- 6) 自动派发 mousedown 触发下载 ----
        print("[CLICK] 派发 mousedown 触发 NDM 下载...")
        try:
            result = page.evaluate(NDM_PANEL_CLICK_JS)
        except Exception as e:
            result = {"ok": False, "reason": str(e)}

        if result.get("ok"):
            print(f"[CLICK] OK: {result.get('results')}")
            print(f"[WAIT] 等待 {POST_CLICK_WAIT}s 让 NDM 处理...")
            time.sleep(POST_CLICK_WAIT)
            print("[DONE] 下载任务已提交到 NDM。")
        else:
            print(f"[CLICK] 失败: {result}")
            print("        你可以手动点击页面上的 NDM 浮窗。")

        print()
        print("按回车键关闭浏览器并清理临时文件。")
        input()
        return 0

    except Exception as exc:
        print(f"[FATAL] {type(exc).__name__}: {exc}")
        return 1

    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
            time.sleep(1.5)

        print()
        print("[CLEANUP] 清理临时文件...")
        cleanup_all(verbose=True)


if __name__ == "__main__":
    raise SystemExit(main())
