"""Правки ставок, которые применяет ActionApplier: корректировки (bid_modifier) и целевая цена конверсии (set_cpa).

Как и для площадок, каждая правка:
  • перед записью перечитывает текущее значение в Директе;
  • после записи перечитывает ещё раз и проверяет, что изменение появилось;
  • запоминает прежнее значение (metrics.previous), чтобы откат вернул ровно его.

Новые типы правок добавляются сюда же: класс с apply/rollback/describe и строка в HANDLERS.
"""
from __future__ import annotations

from ..rules.actions import ActionError

DEVICE_FIELD = {"MOBILE": "MobileAdjustment", "DESKTOP": "DesktopAdjustment", "TABLET": "TabletAdjustment"}
DEVICE_RU = {"MOBILE": "Смартфоны", "DESKTOP": "Компьютеры", "TABLET": "Планшеты"}
GENDER_RU = {"GENDER_MALE": "Мужчины", "GENDER_FEMALE": "Женщины"}
AGE_RU = {"AGE_0_17": "до 18", "AGE_18_24": "18–24", "AGE_25_34": "25–34", "AGE_35_44": "35–44", "AGE_45_54": "45–54",
          "AGE_55": "55+"}
# Стратегии с целевой ценой конверсии: тип → (блок, поле с ценой в микроединицах).
CPA_FIELDS = {"AVERAGE_CPA": ("AverageCpa", "AverageCpa"), "PAY_FOR_CONVERSION": ("PayForConversion", "Cpa")}
MODIFIER_RANGE = (50, 150)  # платформа не выходит за ±50%, даже если в Директе можно больше


def slice_label(slice_name: str, value: str) -> str:
    if slice_name == "device":
        return DEVICE_RU.get(value, value)
    gender, _, age = value.partition("|")
    return f"{GENDER_RU.get(gender, gender)} {AGE_RU.get(age, age)}"


# ------------------------------------------------------------ стратегии ---
def strategy_container(c: dict) -> str | None:
    for key in ("TextCampaign", "UnifiedCampaign"):
        if isinstance(c.get(key), dict) and c[key].get("BiddingStrategy"):
            return key
    return None


def strategy_cpa(strategy: dict | None) -> dict[str, float]:
    """{"Search": 6000.0, "Network": …} — целевая цена в валюте кабинета для сторон со стратегией по CPA."""
    out = {}
    for side in ("Search", "Network"):
        s = (strategy or {}).get(side) or {}
        f = CPA_FIELDS.get(s.get("BiddingStrategyType"))
        if f and (s.get(f[0]) or {}).get(f[1]):
            out[side] = float(s[f[0]][f[1]]) / 1_000_000
    return out


class CpaHandler:
    """metrics: {"new_cpa": {"Search": 5200}, "old_cpa": {"Search": 6000}} — в валюте кабинета."""

    def describe(self, a) -> str:
        m = a.metrics or {}
        parts = [f"{side}: {m.get('old_cpa', {}).get(side)} → {v}" for side, v in (m.get("new_cpa") or {}).items()]
        return "целевая цена конверсии " + "; ".join(parts)

    def _read(self, client, cid: int) -> tuple[dict, str, dict]:
        c = client.get_campaign(cid)
        key = strategy_container(c)
        if not key:
            raise ActionError(f"Директ не вернул стратегию кампании {cid}")
        return c, key, c[key]["BiddingStrategy"]

    def _write(self, client, cid: int, key: str, strategy: dict, values: dict[str, float]) -> None:
        update = {}
        for side, value in values.items():
            s = strategy[side]
            block, field = CPA_FIELDS[s["BiddingStrategyType"]]
            update[side] = {"BiddingStrategyType": s["BiddingStrategyType"], block: {**s[block], field: int(round(value)) * 1_000_000}}
        client.update_campaign({"Id": cid, key: {"BiddingStrategy": update}})

    def apply(self, client, a, cid: int) -> bool:
        m = a.metrics or {}
        new, old = m.get("new_cpa") or {}, m.get("old_cpa") or {}
        _, key, strategy = self._read(client, cid)
        live = strategy_cpa(strategy)
        for side in new:
            if side not in live:
                raise ActionError(f"в кампании {cid} стратегия ({side}) больше не по цене конверсии — пересчитайте")
            if round(live[side]) != round(float(old.get(side, -1))):
                raise ActionError(f"в кампании {cid} целевая цена ({side}) изменилась с {old.get(side)} на {live[side]:.0f} "
                                  "после предложения — пересчитайте правку")
        if all(round(live[s]) == round(float(v)) for s, v in new.items()):
            return False
        self._write(client, cid, key, strategy, {s: float(v) for s, v in new.items()})
        check = strategy_cpa(self._read(client, cid)[2])
        if any(round(check.get(s, -1)) != round(float(v)) for s, v in new.items()):
            raise ActionError(f"Директ принял запрос, но в кампании {cid} целевая цена не изменилась")
        a.metrics = {**m, "previous": {**(m.get("previous") or {}), str(cid): {s: live[s] for s in new}}}
        return True

    def rollback(self, client, a, cid: int) -> None:
        prev = ((a.metrics or {}).get("previous") or {}).get(str(cid))
        if not prev:
            return
        _, key, strategy = self._read(client, cid)
        self._write(client, cid, key, strategy, {s: float(v) for s, v in prev.items() if s in strategy_cpa(strategy)})


# --------------------------------------------------------- корректировки ---
def find_modifier(mods: list[dict], slice_name: str, value: str) -> tuple[int, int] | None:
    """(Id, текущий коэффициент) корректировки для условия или None."""
    for m in mods:
        if slice_name == "device":
            adj = m.get(DEVICE_FIELD.get(value, ""))
            if isinstance(adj, dict) and "BidModifier" in adj:
                return int(m["Id"]), int(adj["BidModifier"])
        else:
            adj = m.get("DemographicsAdjustment")
            gender, _, age = value.partition("|")
            if isinstance(adj, dict) and adj.get("Gender") == gender and adj.get("Age") == age:
                return int(m["Id"]), int(adj["BidModifier"])
    return None


class BidModifierHandler:
    """metrics: {"slice": "device" | "demography", "value": "MOBILE" | "GENDER_FEMALE|AGE_25_34", "modifier": 80}."""

    def describe(self, a) -> str:
        m = a.metrics or {}
        return f"корректировка «{slice_label(m.get('slice', ''), m.get('value', ''))}» {m.get('modifier')}%"

    def _adjustment(self, slice_name: str, value: str, modifier: int) -> dict:
        if slice_name == "device":
            if value not in DEVICE_FIELD:
                raise ActionError(f"неизвестное устройство {value}")
            return {DEVICE_FIELD[value]: {"BidModifier": modifier}}
        gender, _, age = value.partition("|")
        if gender not in GENDER_RU or age not in AGE_RU:
            raise ActionError(f"неизвестная группа {value}")
        return {"DemographicsAdjustments": [{"Gender": gender, "Age": age, "BidModifier": modifier}]}

    def apply(self, client, a, cid: int) -> bool:
        m = a.metrics or {}
        slice_name, value, modifier = m.get("slice"), m.get("value"), int(m.get("modifier", 100))
        if not MODIFIER_RANGE[0] <= modifier <= MODIFIER_RANGE[1]:
            raise ActionError(f"корректировка {modifier}% вне допустимого диапазона {MODIFIER_RANGE}")
        found = find_modifier(client.get_bid_modifiers(cid), slice_name, value)
        if found and found[1] == modifier:
            return False
        if found:
            client.set_bid_modifier(found[0], modifier)
        else:
            client.add_bid_modifier(cid, self._adjustment(slice_name, value, modifier))
        check = find_modifier(client.get_bid_modifiers(cid), slice_name, value)
        if not check or check[1] != modifier:
            raise ActionError(f"Директ принял запрос, но в кампании {cid} корректировка не изменилась")
        a.metrics = {**m, "previous": {**(m.get("previous") or {}), str(cid): found[1] if found else None}}
        return True

    def rollback(self, client, a, cid: int) -> None:
        m = a.metrics or {}
        prev = (m.get("previous") or {}).get(str(cid))
        found = find_modifier(client.get_bid_modifiers(cid), m.get("slice"), m.get("value"))
        if not found:
            return
        if prev is None:
            client.delete_bid_modifier(found[0])
        elif found[1] != int(prev):
            client.set_bid_modifier(found[0], int(prev))


HANDLERS = {"set_cpa": CpaHandler(), "bid_modifier": BidModifierHandler()}
