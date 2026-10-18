/**
 * 首帧之前把主题定下来。
 *
 * React 的入口是 `<script type="module">`，而模块脚本天生 defer：它跑起来时 HTML
 * 已经画过一帧了。深色用户看到的就是一次整窗白闪——那比配色难看更像一个坏掉的程序。
 * 所以这一段不能住在 React 里，只能住在 HTML 之前。
 *
 * **为什么是外部文件而不是 index.html 里的一段内联 `<script>`**：后端给所有响应发的
 * CSP 是 `script-src 'self'`（见 back-end/main.py 的安全头中间件），内联脚本会被浏览器
 * 直接拦掉。现在前端由 Vite 开发服务器与 Electron 的 file:// 提供，撞不上这堵墙，
 * 而哪天把 SPA 改成后端静态挂载时，症状是"深色用户全部闪白"——没有报错、没有日志、
 * 没人会去查的那类坏法。外部同源脚本在三种提供方式下都合法。
 *
 * 与 `src/shared/lib/ThemeContext.tsx` 共用同一份约定：存储键 `app-theme`，生效方式是
 * 给 `<html>` 加 `dark` 类。两处手写迟早分叉，所以这份约定由
 * `src/shared/lib/themeBoot.test.ts` 钉住。
 */
(function themeBoot() {
  var STORAGE_KEY = 'app-theme';
  var root = document.documentElement;

  function systemPrefersDark() {
    // jsdom 与某些老 WebKit 里 matchMedia 可能不存在，而不是返回 false
    return (
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-color-scheme: dark)').matches
    );
  }

  var stored = '';
  try {
    stored = window.localStorage.getItem(STORAGE_KEY) || '';
  } catch (error) {
    // 隐私模式与被禁用的存储：读不到就跟随系统，而不是把所有人锁在浅色上
  }

  // 只认这两个字面值，其余（包括 "system" 与空）一律走系统偏好——与 React 那侧的
  // resolve() 同一个规则，两边各写一遍就会分叉，所以有测试钉着
  var theme =
    stored === 'light' || stored === 'dark' ? stored : systemPrefersDark() ? 'dark' : 'light';

  root.classList.toggle('dark', theme === 'dark');
  // 留下一个可见记号：判断"这一次闪白到底是不是首帧之前没定成"的唯一体面办法，
  // React 不写这个属性，所以它在了就说明这段真的跑在画帧之前
  root.setAttribute('data-theme-boot', theme);
})();
