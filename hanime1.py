from playwright.sync_api import sync_playwright

HEADLESS = False
NDM_EXT = r"C:\Users\Administrator\AppData\Local\Microsoft\Edge\User Data\Default\Extensions\pbghcbaeehloijjcebiflemhcebmlnke\1.9.91_0"
USER_DATA = r"C:\Users\Administrator\Documents\ndm_profile"

pw = sync_playwright().start()
context = pw.chromium.launch_persistent_context(
    USER_DATA,
    headless=HEADLESS,
    channel='msedge',
    args=[
        '--disable-blink-features=AutomationControlled',
        '--no-sandbox',
        '--disable-dev-shm-usage',
        f'--disable-extensions-except={NDM_EXT}',
        f'--load-extension={NDM_EXT}',
    ],
    ignore_default_args=['--enable-automation'],
)
page = context.pages[0] if context.pages else context.new_page()
try:
    page.goto("https://hanime1.com", wait_until='domcontentloaded', timeout=60000)
except Exception as e:
    print(f"页面加载告警（继续运行）: {type(e).__name__}: {e}")
input("按 Enter 键退出程序并关闭浏览器...")
context.close()
pw.stop()
