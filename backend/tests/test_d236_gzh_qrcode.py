"""D236：公众号「一鸣AI编程」二维码落位守卫。

背景：作者要求"把我公众号二维码肯定要放上去"。二维码这类素材最容易出的问题是
**只加在某一个页面**——登录页有、侧栏没有；或者图片路径换了只改了一处，另一处变裂图。

本守卫把"二维码在四处都在"固化成测试：
  ① 素材文件在仓里（且不是 0 字节的占位图）；
  ② 四处落位（侧栏 / 登录页 / 版本信息页 / 手册页）都引用**同一个共用模块** gzh.js；
  ③ 共用模块导出 gzhBlock，且中英两套 i18n 词条齐全（漏一条 → 切英文时露出 key 名）；
  ④ 用到的 class 在 ops*.css 里都有定义（不在的话会渲染成没有样式的裸元素）；
  ⑤ OSS 导出白名单含 static（否则开源包里的二维码会缺图）。

命名沿用仓库约定：test_d<序号>_<主题>.py
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
STATIC = ROOT / "static"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def test_qrcode_asset_exists_and_is_not_empty():
    """素材必须在仓里（OSS 包靠 static/ 目录分发，缺文件 = 线上裂图）。"""
    img = STATIC / "img" / "qrcode-yiming-ai.jpg"
    assert img.exists(), "缺少公众号二维码素材 static/img/qrcode-yiming-ai.jpg"
    assert img.stat().st_size > 2000, "二维码文件过小，疑似占位图"
    # 必须是真 JPEG（SOI 标记 FF D8），避免把 PNG 改名成 .jpg
    assert img.read_bytes()[:2] == b"\xff\xd8", "二维码不是合法 JPEG（SOI 标记缺失）"


def test_gzh_module_is_shared_primitive():
    """四处落位必须共用同一个模块（各写各的 = 图片路径一改就裂）。"""
    mod = STATIC / "js" / "gzh.js"
    assert mod.exists(), "缺少共用模块 static/js/gzh.js"
    src = _read(mod)
    assert "export function gzhBlock(" in src, "gzh.js 未导出 gzhBlock"
    assert "export const GZH_IMG" in src, "gzh.js 未导出图片地址常量"
    assert "/img/qrcode-yiming-ai.jpg" in src, "gzh.js 的图片路径与素材不符"
    # 共用原语必须走 ui.js 的 h()（不手搓 innerHTML）与 i18n 的 t()（不写死中文）
    assert 'from "./ui.js"' in src and 'from "./i18n.js"' in src, "gzh.js 未复用 ui.js / i18n.js"
    assert "t(" in src, "gzh.js 文案未走 i18n（切英文会露出中文）"


def test_qrcode_placed_in_all_four_surfaces():
    """侧栏 / 登录页 / 版本信息页 / 手册页 四处都要有。"""
    places = {
        "侧栏（index.html）": STATIC / "index.html",
        "登录页": STATIC / "js" / "pages" / "login.js",
        "版本信息页": STATIC / "js" / "pages" / "about.js",
        "手册页": STATIC / "js" / "pages" / "manual.js",
    }
    for name, f in places.items():
        src = _read(f)
        assert "gzhBlock" in src, f"{name} 没有引用 gzhBlock（二维码没落位）"
        assert "gzh.js" in src, f"{name} 没有 import 共用模块 gzh.js"


def test_i18n_has_both_languages():
    """中英都要有词条——漏一条，切到 EN 就会显示 key 名（t() 的兜底行为）。"""
    src = _read(STATIC / "js" / "i18n.js")
    keys = ["gzh.alt", "gzh.title", "gzh.sub", "gzh.cta", "gzh.star",
            "gzh.short_title", "gzh.short_cta"]
    zh_part, en_part = src.split("const EN = {", 1)
    for k in keys:
        assert f'"{k}"' in zh_part, f"i18n 缺中文词条 {k}"
        assert f'"{k}"' in en_part, f"i18n 缺英文词条 {k}"


def test_sidebar_qr_repaints_when_language_lands():
    """侧栏二维码必须能按"语言落定"重绘。

    实测问题：外壳首屏渲染时服务端 preferred_lang 还没到（/api/auth/me 是异步的），
    侧栏二维码会先用 localStorage 的语言画一次；随后语言落定，它却还停在旧语言——
    于是出现"整页中文、侧栏英文"的混搭。
    修法：i18n 在语言真正落定时派发 ops:lang 事件，侧栏监听后重绘。
    """
    i18n = _read(STATIC / "js" / "i18n.js")
    assert "ops:lang" in i18n, "i18n 没有派发 ops:lang 事件（侧栏二维码无法随语言重绘）"
    idx = _read(STATIC / "index.html")
    assert "ops:lang" in idx, "index.html 没有监听 ops:lang（侧栏二维码会停在旧语言）"
    assert "paintSidebarGzh" in idx, "index.html 缺少重绘函数 paintSidebarGzh"


def test_gzh_css_classes_are_defined():
    """gzh.js 里用到的 class 必须在 ops*.css 有定义（幽灵类名 = 没有样式的裸元素）。"""
    css = ""
    for cf in sorted(STATIC.glob("ops*.css")):
        css += re.sub(r"/\*.*?\*/", "", _read(cf), flags=re.S)
    src = _read(STATIC / "js" / "gzh.js")
    used = set()
    for m in re.finditer(r'class:\s*`([^`]+)`|class:\s*"([^"]+)"', src):
        for name in (m.group(1) or m.group(2) or "").split():
            if re.fullmatch(r"[a-z][\w-]*", name):
                used.add(name)
    # 动态拼出来的尺寸档（gzh-sm/md/lg）不在源码字面量里，单独校验
    for size in ("sm", "md", "lg"):
        used.add(f"gzh-{size}")
    missing = sorted(c for c in used if f".{c}" not in css)
    assert not missing, f"gzh 相关 class 在 CSS 里没有定义：{missing}"


def test_oss_export_ships_the_asset():
    """OSS 包靠 static/ 白名单分发，静态资源必须随包出去。"""
    export = _read(ROOT / "tools" / "oss_export.py")
    assert '"static"' in export, "OSS 导出白名单里没有 static（二维码不会进开源包）"


# ── 「扫得出来」守卫：二维码贴上去不等于能用 ─────────────────────────────
#
# 实测（本机 cv2.QRCodeDetector 逐档解码 + 浏览器渲染态截图解码）：
#   ① 码面 ≤96px 解不出；104px 勉强；112px 会抖；≥134px 才稳定
#      （一个码模块至少要 3px 宽，低于这个扫码器直接放弃）。
#   ② CSS 是 border-box：`.gzh-img` 的 padding 吃在盒子里 ——
#      "看起来 136px"的码，码面其实只有 122px。**按盒子尺寸断言会放过问题**。
#   ③ border-radius 会削掉二维码的三个定位角（回字码角），
#      扫码器靠定位角定位，削掉就废——渲染态实测：有圆角解不出、去圆角原样解出。
# 所以这里断言的是"**码面**"（盒子 − 2×padding − 2×border），不是盒子。
MIN_CODE_PX = 130      # 码面下限（px）
_PADDING_BORDER = 14   # 各档统一 padding 6 + border 1 → 2×(6+1)


def _tier_code_px(css: str, tier: str) -> int:
    """从 CSS 里取某一档的码面像素数。"""
    m = re.search(rf"\.{tier} \.gzh-img \{{[^}}]*width:\s*(\d+)px", css)
    assert m, f"CSS 里找不到 .{tier} .gzh-img 的尺寸（改样式时别把码面改小了）"
    box = int(m.group(1))
    return box - _PADDING_BORDER


def test_inline_qr_code_face_is_big_enough_to_scan():
    """内联档（登录页 / 版本信息页 / 手册页）的**码面**必须够大，否则贴了也扫不出。"""
    css = ""
    for cf in sorted(STATIC.glob("ops*.css")):
        css += re.sub(r"/\*.*?\*/", "", _read(cf), flags=re.S)
    for tier in ("gzh-md", "gzh-lg"):
        code = _tier_code_px(css, tier)
        assert code >= MIN_CODE_PX, (
            f"{tier} 的码面只有 {code}px（盒子 − padding − border），"
            f"实测 <134px 扫码器会失败；至少要 {MIN_CODE_PX}px")


def test_qr_image_has_no_rounded_corners():
    """二维码图片**不能有圆角**——圆角削掉定位角，扫码器找不到码。"""
    css = ""
    for cf in sorted(STATIC.glob("ops*.css")):
        css += re.sub(r"/\*.*?\*/", "", _read(cf), flags=re.S)
    for sel in (".gzh-img", ".gzh-drawer-img"):
        m = re.search(rf"{re.escape(sel)} \{{([^}}]*)\}}", css)
        assert m, f"CSS 里找不到 {sel}"
        body = m.group(1)
        r = re.search(r"border-radius:\s*([0-9.]+)px", body)
        assert r is None or float(r.group(1)) == 0, (
            f"{sel} 有 border-radius（{r.group(1) if r else '?'}px）——"
            "圆角会削掉二维码的定位角，实测扫码直接失败")


def test_drawer_enlarge_qr_is_scannable():
    """窄位（侧栏）走"小图 + 点击放大"，抽屉里的码面必须够大。"""
    src = _read(STATIC / "js" / "gzh.js")
    m = re.search(r"GZH_LARGE_PX\s*=\s*(\d+)", src)
    assert m, "gzh.js 未导出 GZH_LARGE_PX"
    code = int(m.group(1)) - 10   # padding 4 + border 1 → 2×(4+1)
    assert code >= MIN_CODE_PX, f"抽屉里的码面只有 {code}px，扫不出来"
    assert "openGzhDrawer" in src, "gzh.js 缺放大抽屉（窄位用户没法扫码）"
    assert "gzh-img-zoom" in src, "gzh.js 没给小图加放大态类名"


def test_qr_actually_decodes_at_configured_size():
    """有 cv2 就真解一次：把素材缩到各档码面，必须能解出公众号链接。

    没有 cv2 的环境（CI 精简镜像）自动跳过——静态断言已覆盖尺寸与圆角。
    """
    cv2 = pytest.importorskip("cv2", reason="未装 opencv，跳过真实解码校验")
    np = pytest.importorskip("numpy")
    img_path = STATIC / "img" / "qrcode-yiming-ai.jpg"
    src = cv2.imread(str(img_path))
    assert src is not None, "素材读不出来（不是合法图片）"
    det = cv2.QRCodeDetector()
    css = ""
    for cf in sorted(STATIC.glob("ops*.css")):
        css += re.sub(r"/\*.*?\*/", "", _read(cf), flags=re.S)
    sizes = {t: _tier_code_px(css, t) for t in ("gzh-md", "gzh-lg")}
    for tier, size in sizes.items():
        small = cv2.resize(src, (size, size), interpolation=cv2.INTER_AREA)
        data, _, _ = det.detectAndDecode(small)
        assert data and "weixin.qq.com" in data, (
            f"{tier} 码面 {size}px 解不出公众号链接（实际解出：{data!r}）——"
            "尺寸或圆角被改坏了")
