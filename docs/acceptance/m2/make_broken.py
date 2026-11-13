"""Собирает сломанную копию эталона Ozon для сценария C приёмки M2 (docs/acceptance/m2.md, раздел 2.4).

Поломка: путь внутреннего API следующих страниц выдачи `.../page/json/v2` заменён на
несуществующий `.../page/json/v3`, версия скрипта на 1 больше эталона.

    python docs/acceptance/m2/make_broken.py      # перезаписывает ozon-search-cards-broken.py
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOOD = HERE.parents[2] / "examples" / "ozon-search-cards" / "script.py"
BROKEN = HERE / "ozon-search-cards-broken.py"

API_OK = '"/api/entrypoint-api.bx/page/json/v2" in response.url'
API_BROKEN = '"/api/entrypoint-api.bx/page/json/v3" in response.url'


def build(good: str) -> str:
    if good.count(API_OK) != 1:
        raise SystemExit(f"в эталоне не найдено ровно одно место `{API_OK}`: поломку нужно выбрать заново")
    m = re.search(r"^# version = (\d+)$", good, re.M)
    if m is None:
        raise SystemExit("в манифесте эталона нет строки `# version = N`")
    broken = good.replace(API_OK, API_BROKEN)
    return broken[: m.start()] + f"# version = {int(m.group(1)) + 1}" + broken[m.end() :]


if __name__ == "__main__":
    BROKEN.write_text(build(GOOD.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"записано: {BROKEN}")
