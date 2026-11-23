// gzh.js —— 公众号「一鸣AI编程」二维码区块（**共用原语**）
//
// 为什么单独成模块：登录页、侧栏、版本信息页、使用手册页四处都要放同一张码。
// 各写各的会出现"一处有图、一处只剩一句文字"的漂移（与 D86「漏 import 导致静默失效」同类）：
// 图片路径一改就四处失效，只有真正共用的一处会被想起来。
//
// 纪律（沿用《前端风格规范》）：
//   ① 只用 ui.js 的 h() 造元素，不用 innerHTML 拼字符串；
//   ② 文案全部走 i18n 的 t()，中英两套词条都要有；
//   ③ 尺寸靠 class（gzh-sm / gzh-md / gzh-lg）分级，不在 JS 里写死像素。
//
// D236-补：**二维码要"扫得出来"才算数**。
// 实测（本机 cv2.QRCodeDetector 逐档解码）：把素材缩到 ≤96px 就解不出来了，
// 104px 勉强、112px 会抖、≥120px 才稳定——屏幕上一个模块至少要 3px 宽。
// 而 CSS 是 border-box：`.gzh-img` 的 padding 吃在尺寸里，所以"看起来 96px"的码
// 实际只有 88px，扫不出来。故：
//   · 内联档（md / lg）直接按**内容 ≥120px** 给尺寸，一次扫码成功；
//   · 侧栏窄（232px）放不下 120px 的码还常驻，改成"小图 + 点击放大"，
//     点开抽屉里是 260px 的大码（内容 252px），同样一次扫成。
import { h } from "./ui.js";
import { drawer } from "./ui.js";
import { t } from "./i18n.js";

/** 二维码图片地址（静态资源，OSS 包同样分发）。 */
export const GZH_IMG = "/img/qrcode-yiming-ai.jpg";

/** 开源仓库地址——"给仓库点 Star" 的落点。 */
export const GZH_REPO = "https://github.com/qsumm/TestOPS";

/** 放大抽屉里的二维码边长（px）——比内联档更大，扫码最稳。 */
export const GZH_LARGE_PX = 260;   // 抽屉里的码面 250px（实测原样可解）

/**
 * 生成二维码区块。
 * @param {{size?: "sm"|"md"|"lg", cta?: boolean, showTitle?: boolean, wrap?: string, enlarge?: boolean}} opts
 *   size：sm=侧栏紧凑 / md=登录页、版本信息页 / lg=手册页
 *   cta：是否附带「给仓库点 Star」链接（默认 true）
 *   showTitle：是否显示标题行（卡片自带 h3 时传 false，避免同一句话出现两次）
 *   wrap：外层包裹 class（如 "sf-gzh" / "login-gzh"），不传则直接返回区块本身
 *   enlarge：小图是否可点击放大（侧栏用；内联档本身够大，不需要）
 */
export function gzhBlock(opts = {}) {
  const { size = "md", cta = true, showTitle = true, wrap = "", enlarge = false } = opts;
  const compact = size === "sm";
  const img = h("img", { class: "gzh-img", src: GZH_IMG, alt: t("gzh.alt"), loading: "lazy" });
  if (enlarge) {
    img.classList.add("gzh-img-zoom");
    img.setAttribute("role", "button");
    img.setAttribute("tabindex", "0");
    img.title = t("gzh.enlarge_hint");
    const open = () => openGzhDrawer();
    img.addEventListener("click", open);
    img.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  }
  const inner = h("div", { class: `gzh gzh-${size}` },
    img,
    h("div", { class: "gzh-meta" },
      showTitle
        ? h("div", { class: "gzh-title" }, compact ? t("gzh.short_title") : t("gzh.title"))
        : null,
      compact ? null : h("div", { class: "gzh-sub" }, t("gzh.sub")),
      h("div", { class: "gzh-cta" }, compact ? t("gzh.short_cta") : t("gzh.cta")),
      compact
        ? h("div", { class: "gzh-zoom-hint" }, t("gzh.enlarge_hint"))
        : null,
      cta && !compact
        ? h("a", { class: "gzh-star", href: GZH_REPO, target: "_blank", rel: "noopener" },
          t("gzh.star"))
        : null));
  return wrap ? h("div", { class: wrap }, inner) : inner;
}

/** 放大抽屉：大尺寸二维码 + 完整文案——窄位（侧栏）扫不出来时的兜底出口。 */
export function openGzhDrawer() {
  drawer(t("gzh.title"), { width: 420 },
    h("div", { class: "gzh-drawer" },
      h("img", { class: "gzh-drawer-img", src: GZH_IMG, alt: t("gzh.alt") }),
      h("div", { class: "gzh-meta" },
        h("div", { class: "gzh-title" }, t("gzh.title")),
        h("div", { class: "gzh-sub" }, t("gzh.sub")),
        h("div", { class: "gzh-cta" }, t("gzh.cta")),
        h("a", { class: "gzh-star", href: GZH_REPO, target: "_blank", rel: "noopener" },
          t("gzh.star")))));
}
