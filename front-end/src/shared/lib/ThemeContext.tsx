import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

/**
 * 主题：浅色 / 深色 / 跟随系统。
 *
 * 三态而不是两态，是因为这个应用是坐席整天开着的桌面端：白天值班台是浅的
 * （和打印出来的工单一模一样），夜班机房是暗的，而"跟着系统走"是那些
 * 电脑已经被公司统一策略管着的人唯一能接受的选项——他们不希望应用
 * 和自己的操作系统长得不一样。
 */
export type ThemeMode = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "app-theme";

/**
 * 循环顺序与显示名。**导出**而不是各页面自己排：三处各写一遍"浅→深→跟随系统"
 * 迟早会分叉，而分叉的表现是同一个应用里点同一个按钮往三个方向转。
 */
export const NEXT_MODE: Record<ThemeMode, ThemeMode> = {
  light: "dark",
  dark: "system",
  system: "light",
};

export const THEME_LABELS: Record<ThemeMode, string> = {
  light: "浅色",
  dark: "深色",
  system: "跟随系统",
};

interface ThemeContextType {
  /** 用户的选择，可能是 "system" */
  mode: ThemeMode;
  /** 实际生效的那一个，组件与图标都用它 */
  theme: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
  /** 顶栏那颗按钮的循环顺序：浅色 → 深色 → 跟随系统 */
  cycleMode: () => void;
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

function readStoredMode(): ThemeMode {
  const saved = localStorage.getItem(STORAGE_KEY);
  return saved === "light" || saved === "dark" || saved === "system"
    ? saved
    : "system";
}

function systemPrefersDark(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

function resolve(mode: ThemeMode): ResolvedTheme {
  if (mode === "system") return systemPrefersDark() ? "dark" : "light";
  return mode;
}

/**
 * 在 React 挂载之前上一次类名。**这是兜底，不是主路径**。
 *
 * 主路径是 `public/theme-boot.js`：它由 index.html 以经典脚本加载，在 body 画帧之前
 * 就跑完了。放在这里而不是只靠那个文件，是因为 `main.tsx` 是模块脚本（天生 defer），
 * 而引导脚本万一没跑成——存储被禁用、静态资源 404、CSP 改动——界面至少要跟
 * `ThemeProvider` 挂起来时算出的那一份一致，否则会出现"React 以为深色、屏幕是浅色"。
 *
 * 不做任何一处的话，每次启动都会先以 `:root`（浅色）画一帧再翻成深色——桌面端整窗
 * 闪一下白，比配色不好看更像一个坏掉的程序。
 */
export function applyStoredThemeBeforeMount() {
  const root = document.documentElement;
  root.classList.toggle("dark", resolve(readStoredMode()) === "dark");
}

export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [mode, setModeState] = useState<ThemeMode>(readStoredMode);
  const theme = resolve(mode);

  useEffect(() => {
    const root = document.documentElement;
    const dark = resolve(mode) === "dark";
    const apply = () => root.classList.toggle("dark", dark);

    /**
     * View Transitions 只当**动画**用，正确性不交给它。
     *
     * 页面不可见时（后台标签、最小化的桌面窗口）视图过渡可能被推迟甚至跳过，
     * 于是 `apply` 根本不跑——表现是"点了深色但界面没变"，而且只在看不见的窗口里
     * 发生，最难复现。所以留一个兜底：短定时器到点还没执行就直接落类名。
     * 动画本身照常，因为可见时 `apply` 走的是视图过渡那条路。
     */
    let applied = false;
    const doc = document as Document & {
      startViewTransition?: (update: () => void) => unknown;
    };
    if (typeof doc.startViewTransition === "function") {
      doc.startViewTransition(() => {
        applied = true;
        apply();
      });
      window.setTimeout(() => {
        if (!applied) apply();
      }, 60);
    } else {
      apply();
    }

    localStorage.setItem(STORAGE_KEY, mode);
  }, [mode]);

  /**
   * "跟随系统"必须真的跟着：系统夜间模式一开，值班台就跟着翻。
   * 只在 mode === "system" 时挂监听——手动选浅色的人不该被系统改主意。
   */
  useEffect(() => {
    if (mode !== "system") return;
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setModeState("system"); // 重新求解，触发上面的 apply
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [mode]);

  const setMode = useCallback((next: ThemeMode) => setModeState(next), []);

  const cycleMode = useCallback(() => setModeState((prev) => NEXT_MODE[prev]), []);

  const value = useMemo(
    () => ({ mode, theme, setMode, cycleMode }),
    [mode, theme, setMode, cycleMode]
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
};

export const useTheme = () => {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return context;
};
