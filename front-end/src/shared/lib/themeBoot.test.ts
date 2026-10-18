import fs from "node:fs";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

/**
 * 首帧之前的那一次主题应用。
 *
 * 这段逻辑故意**不在 React 里**：`main.tsx` 是 `<script type="module">`，模块脚本天生
 * defer，跑起来时 HTML 已经画过一帧——深色用户就看到一次整窗白闪。所以它住在
 * `public/theme-boot.js`，由 `index.html` 以经典脚本在 body 之前加载。
 *
 * 于是这里钉三件事，每一件都是一个会静默复发的坏法：
 *
 * 1. **接线**。引导脚本必须还在、必须是经典脚本（`type="module"` 就等于把它退回
 *    defer，白闪原样回来，而没有任何测试会因此变红——除了这一条）。
 * 2. **两份实现说的是同一份约定**。存储键与类名各写一遍迟早分叉，而分叉的表现是
 *    "引导脚本定了深色、React 以为浅色"，只在启动那一刻闪一下，最难被复现。
 * 3. **不许改成内联**。后端 CSP 是 `script-src 'self'`，内联脚本会被浏览器拦掉；
 *    现在的前端由 Vite 与 Electron 的 file:// 提供，撞不上这堵墙，所以这个坑只有
 *    改成后端静态挂载那天才会露出来——而那时没有报错。
 */

// jsdom 环境里 import.meta.url 不是 file: URL（fileURLToPath 会直接抛），而 vitest
// 的工作目录就是 front-end/（配置文件与 include 模式都在那儿），所以从 cwd 推
const FRONT_END_ROOT = process.cwd();
const BOOT_PATH = path.join(FRONT_END_ROOT, "public", "theme-boot.js");
const HTML_PATH = path.join(FRONT_END_ROOT, "index.html");
const CONTEXT_PATH = path.join(FRONT_END_ROOT, "src", "shared", "lib", "ThemeContext.tsx");
// 跨到后端去读一条头，是因为这条约定**本身就是跨仓库边界的**：改 CSP 的人与改
// index.html 的人不是同一个，而两边都不认为自己那次会让深色用户闪白
const BACKEND_MAIN_PATH = path.resolve(FRONT_END_ROOT, "..", "back-end", "main.py");

const bootSource = fs.readFileSync(BOOT_PATH, "utf8");
const html = fs.readFileSync(HTML_PATH, "utf8");
const contextSource = fs.readFileSync(CONTEXT_PATH, "utf8");

function runBootScript() {
  // 引导脚本是个 IIFE，没有导出。用 new Function 在 jsdom 的全局里执行它，
  // 测的就是"浏览器解析到那一行时会发生什么"，而不是我抄出来的第二份逻辑
  new Function(bootSource)();
}

function resetHtml() {
  document.documentElement.className = "";
  document.documentElement.removeAttribute("data-theme-boot");
  window.localStorage.clear();
  vi_restoreMatchMedia();
}

let matchMediaCleanup: (() => void) | null = null;

function vi_stubMatchMedia(prefersDark: boolean) {
  const original = window.matchMedia;
  // @ts-expect-error 测试里只需要 matches 这一个字段
  window.matchMedia = () => ({ matches: prefersDark });
  matchMediaCleanup = () => {
    window.matchMedia = original;
    matchMediaCleanup = null;
  };
}

function vi_restoreMatchMedia() {
  if (matchMediaCleanup) matchMediaCleanup();
}

afterEach(() => {
  resetHtml();
});

/**
 * 去掉 HTML 注释之后再找标签。
 *
 * 不这么做的话这条守卫是假的：注释里写着"public/theme-boot.js"和"`type=\"module\"`
 * 就等于退回 defer"，于是 `indexOf('theme-boot.js')` 命中注释，切出来的那段既不是
 * 真标签、也不含被禁的那个属性——把 index.html 改成 `<script type="module">` 之后
 * 测试照样绿。这一条是写完才发现的，不是设计出来的。
 */
const markup = html.replace(/<!--[\s\S]*?-->/g, "");

function bootTag(): string | null {
  const found = /<script\b[^>]*theme-boot\.js[^>]*>/.exec(markup);
  return found ? found[0] : null;
}

describe("主题引导脚本的接线", () => {
  it("index.html 在模块入口之前以经典脚本加载它", () => {
    const tag = bootTag();
    expect(tag, "index.html 里找不到加载 public/theme-boot.js 的脚本标签").toBeTruthy();

    // 经典脚本才会阻塞解析并在 body 画帧之前跑完。加上 type="module" 的那一刻，
    // 它就被降级成 defer，白闪原样回来——而界面看起来"只是审美差了一点"
    expect(tag).not.toContain('type="module"');
    expect(tag).not.toContain("defer");
    expect(tag).not.toContain("async");

    const moduleAt = markup.indexOf('src="/src/app/main.tsx"');
    expect(moduleAt).toBeGreaterThanOrEqual(0);
    expect(markup.indexOf(tag!)).toBeLessThan(moduleAt);
  });

  it("保持外部文件而不是内联，因为 CSP 不给内联脚本活路", () => {
    const cspLine = fs
      .readFileSync(BACKEND_MAIN_PATH, "utf8")
      .split("\n")
      .find((line) => line.includes("Content-Security-Policy"));
    expect(cspLine).toBeTruthy();
    expect(cspLine).toContain("script-src 'self'");
    expect(cspLine).not.toContain("script-src 'self' 'unsafe-inline'");

    // 反过来的检查：有人图省事把逻辑抄回 index.html 时，这条要响。查 markup 而不是
    // 原文，理由见上面那段——注释里有同一个字符串
    expect(markup).not.toContain("app-theme");
  });
});

describe("两份实现必须说同一份约定", () => {
  it("存储键与 ThemeContext 用的是同一个字面值", () => {
    expect(bootSource).toContain("'app-theme'");
    expect(contextSource).toContain('"app-theme"');
  });

  it("生效方式都是给 <html> 上 dark 类", () => {
    expect(bootSource).toContain("classList.toggle('dark'");
    expect(contextSource).toContain('classList.toggle("dark"');
  });

  it("两边都把认不出来的值交给系统偏好，而不是猜一个", () => {
    // React 那侧：非 light/dark 走 resolve→systemPrefersDark
    expect(contextSource).toContain('if (mode === "system") return systemPrefersDark()');
    // 引导脚本那侧：同样的规则，只是写成三元
    expect(bootSource).toContain("stored === 'light' || stored === 'dark'");
  });
});

describe("引导脚本跑起来之后", () => {
  it("存了深色就上 dark 类并留下记号", () => {
    window.localStorage.setItem("app-theme", "dark");
    runBootScript();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.getAttribute("data-theme-boot")).toBe("dark");
  });

  it("存了浅色就不上 dark 类", () => {
    window.localStorage.setItem("app-theme", "light");
    vi_stubMatchMedia(true); // 系统要深色也不理：用户明确选过浅色
    runBootScript();
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.getAttribute("data-theme-boot")).toBe("light");
  });

  it("没存过的时候跟随系统", () => {
    vi_stubMatchMedia(true);
    runBootScript();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("存储被禁用时不炸，按系统偏好走", () => {
    vi_stubMatchMedia(true);
    const original = window.localStorage;
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError: storage disabled");
      },
    });
    try {
      runBootScript();
    } finally {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        value: original,
      });
    }
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("matchMedia 不存在时退到浅色而不是抛", () => {
    const original = window.matchMedia;
    // @ts-expect-error 老 WebKit 上它真的可能没有
    delete window.matchMedia;
    try {
      runBootScript();
    } finally {
      window.matchMedia = original;
    }
    expect(document.documentElement.getAttribute("data-theme-boot")).toBe("light");
  });
});
