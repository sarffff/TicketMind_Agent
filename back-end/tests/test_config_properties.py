"""`Settings` 上那些算出来的值（`@property`）每一个都得真的取得出来。

为什么单独立一条守卫：这类属性没有测试、也没有调用点检查，装饰器写错一次不会让
导入失败，只会让**用到它的那条路径在第一次真跑时才炸**。2026-10-17 摸到一例：
`judge_model` 上方多了一个空挂的 `@property`，于是它被装饰两次，`settings.judge_model`
当场 `TypeError: 'property' object is not callable`——表现是 `--judge` 那条评估路径
从没能跑通过，而这在评审里看不见：两行 `@property` 挨着放，完全像手滑。
"""
import pytest

from config import Settings, settings


def _properties() -> dict[str, property]:
    return {name: attr for name, attr in vars(Settings).items() if isinstance(attr, property)}


def test_每个property属性都能真的取到值():
    broken = []
    for name in sorted(_properties()):
        try:
            getattr(settings, name)
        except Exception as exc:  # noqa: BLE001 - 这条守卫就是要接住任何形状坏掉
            broken.append(f"{name}: {type(exc).__name__}: {exc}")
    assert broken == []


def test_这条守卫检查的不是零个属性():
    """一条"通过时什么都不检查"的守卫和它工作时长得一模一样。

    属性要是被批量删光，上一条测试会漂亮地绿着——而那时它已经不再保护任何东西。
    """
    props = _properties()
    # 2026-10-17 写这条时有 6 个。下限留 5：正常增删不该把它变成阻碍，
    # 但"属性被批量删光、这条守卫从此什么都不检查"必须能被它自己发现
    assert len(props) >= 5, f"只发现 {len(props)} 个属性，这条守卫基本是空的"
    # 抽查那几个"回退链"：它们的存在意义就是回退，取不到值等于整条链路失去默认模型
    for name in ("judge_model", "utility_model", "embedding_api_key", "embedding_base_url"):
        assert name in props, f"{name} 不再是 property，检查它的必要性与这条测试的覆盖面"


@pytest.mark.parametrize("name", sorted(_properties()))
def test_每个属性单独点名取值(name):
    """逐个用例是给人看的：炸的时候测试名直接说出是哪个属性，不用再去读断言列表。"""
    getattr(settings, name)
