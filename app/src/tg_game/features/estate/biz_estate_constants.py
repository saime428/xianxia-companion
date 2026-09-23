MINIAPP_SAFETY_BOUNDARY = (
    "只读同步会临时请求 Telegram WebView、xianxia-dwelling/start、details 和 external；"
    "未执行升级/寻宝/布置/宝阁等消耗动作；不保存 initData/tgWebAppData/hash/user/raw URL。"
)
MINIAPP_HUNT_SAFETY_BOUNDARY = (
    "自动寻宝会临时请求一次 Telegram WebView，并在同一洞府 MiniApp 会话内连续寻宝；"
    "默认每轮耗尽神识后结算，达到今日次数上限后停止；"
    "不保存 initData/tgWebAppData/hash/user/raw URL/sessionId。"
)
ESTATE_MINIAPP_DEFAULT_BOT_USERNAME = "fanrenxiuxian_bot"
ESTATE_MINIAPP_FALLBACK_START_PARAM_ENV = (
    "TG_GAME_ESTATE_MINIAPP_FALLBACK_START_PARAM"
)
ESTATE_MINIAPP_FALLBACK_URL_ENV = "TG_GAME_ESTATE_MINIAPP_FALLBACK_URL"
ESTATE_MINIAPP_PUBLIC_ENTRY_CHANNEL = -1002083016447
ESTATE_MINIAPP_PUBLIC_ENTRY_STATE_KEY = "estate_public_entry_discovery"
# 手动指定去哪个群找洞府公共入口。换群后新群没有入口时用它把入口留在旧群：
# 这条路只读历史消息抠 miniapp 启动参数，开小程序时 peer 打的是 bot，
# 所以指令收发照常走当前绑定群，不受影响。
ESTATE_MINIAPP_PUBLIC_ENTRY_OVERRIDE_STATE_KEY = "estate_public_entry_chat_id"
ESTATE_MINIAPP_DEFAULT_API_BASE_URL = "https://asc.aiopenai.app"
ESTATE_MINIAPP_WEB_PATH = "/miniapp/xianxia-dwelling"
ESTATE_MINIAPP_API_PATH_PREFIX = "/api/miniapp/xianxia-dwelling/"
ESTATE_MINIAPP_ENDPOINTS = {
    "start": f"{ESTATE_MINIAPP_API_PATH_PREFIX}start",
    "details": f"{ESTATE_MINIAPP_API_PATH_PREFIX}details",
    "external": f"{ESTATE_MINIAPP_API_PATH_PREFIX}external",
    "journey": f"{ESTATE_MINIAPP_API_PATH_PREFIX}journey",
    "hunt": f"{ESTATE_MINIAPP_API_PATH_PREFIX}hunt",
    "hunt_reveal": f"{ESTATE_MINIAPP_API_PATH_PREFIX}hunt/reveal",
    "hunt_settle": f"{ESTATE_MINIAPP_API_PATH_PREFIX}hunt/settle",
}
ESTATE_MINIAPP_ALLOWED_WEB_HOSTS = {"t.me", "telegram.me", "asc.aiopenai.app"}
ESTATE_MINIAPP_ALLOWED_API_HOSTS = {"asc.aiopenai.app"}
