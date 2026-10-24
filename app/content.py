"""Загрузчик приватного контента из CONTENT_DIR."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.config import settings

SCALAR_KEYS = (
    "brand",
    "payment_description",
    "start",
    "description",
    "faq",
    "rules",
    "support",
    "menu_hint",
    "ask_name",
    "ask_email",
    "ask_phone",
    "invalid_email",
    "invalid_phone",
    "cohort_monthly",
    "cohort_3m",
    "subscription_inactive",
    "subscription_inactive_until",
    "subscription_active",
    "tariff_choice",
    "cancel_confirm",
    "cancel_done",
    "subscription_ended",
    "cancel_yes",
    "cancel_no",
    "card_change",
    "card_change_button",
    "join_not_paid",
    "out_of_flow_ack",
    "admin_reply_hint",
    "admin_request_sent",
    "admin_already",
    "admin_rejected",
    "reminder_3d_template",
    "reminder_1d_template",
    "payment_success",
    "payment_success_button",
    "charge_failed_2h",
    "charge_failed_24h",
    "payment_failed",
    "subscription_expired",
)
MAPPING_KEYS = ("reminder_3d", "reminder_1d")
REQUIRED_KEYS = SCALAR_KEYS + MAPPING_KEYS


class ContentError(RuntimeError):
    """Контент отсутствует, неполный или неверного типа."""


TARIFF_FIELDS = (
    "code",
    "title",
    "price_kopecks",
    "period_days",
    "is_legacy",
    "is_active",
    "sort_order",
)
TARIFF_PERIODS = (30, 90, 180)


@dataclass(frozen=True)
class TariffData:
    """Тариф из content/tariffs.yaml."""

    code: str
    title: str
    price_kopecks: int
    period_days: int
    is_legacy: bool
    is_active: bool
    sort_order: int


_CACHE: dict[str, Any] | None = None
_TARIFFS_CACHE: list[TariffData] | None = None


def _content_path(filename: str) -> Path:
    directory = os.environ.get("CONTENT_DIR") or settings.content_dir
    return Path(directory) / filename


def _read_yaml(filename: str) -> tuple[object, Path]:
    path = _content_path(filename)
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), path
    except FileNotFoundError:
        raise ContentError(f"Не найден файл контента: {path}") from None
    except yaml.YAMLError as exc:
        raise ContentError(f"{path}: некорректный YAML: {exc}") from None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate(data: object, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContentError(f"{path}: ожидается YAML-словарь")
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ContentError(f"{path}: отсутствуют обязательные ключи: {', '.join(missing)}")
    for key in SCALAR_KEYS:
        if not isinstance(data[key], str):
            raise ContentError(f"{path}: ключ '{key}' должен быть строкой")
    for key in MAPPING_KEYS:
        value = data[key]
        if not isinstance(value, dict) or any(
            not isinstance(k, int) or not isinstance(v, str) for k, v in value.items()
        ):
            raise ContentError(f"{path}: ключ '{key}' должен быть словарём int → str")
    return data


def load_texts() -> dict[str, Any]:
    """Загружает и кэширует texts.yaml; проверяет обязательные ключи."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    raw, path = _read_yaml("texts.yaml")
    _CACHE = _validate(raw, path)
    return _CACHE


def _validate_tariffs(data: object, path: Path) -> list[TariffData]:
    if not isinstance(data, list):
        raise ContentError(f"{path}: ожидается YAML-список тарифов")
    tariffs: list[TariffData] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        where = f"{path}: тариф #{index + 1}"
        if not isinstance(item, dict):
            raise ContentError(f"{where}: ожидается словарь")
        missing = [key for key in TARIFF_FIELDS if key not in item]
        if missing:
            raise ContentError(f"{where}: отсутствуют поля: {', '.join(missing)}")
        code = item["code"]
        if not isinstance(code, str) or not code.strip():
            raise ContentError(f"{where}: 'code' должен быть непустой строкой")
        where = f"{path}: тариф '{code}'"
        if code in seen:
            raise ContentError(f"{where}: дубликат code")
        seen.add(code)
        title = item["title"]
        if not isinstance(title, str) or not title.strip():
            raise ContentError(f"{where}: 'title' должен быть непустой строкой")
        price = item["price_kopecks"]
        if not _is_int(price) or price <= 0:
            raise ContentError(f"{where}: 'price_kopecks' должен быть положительным целым")
        period = item["period_days"]
        if not _is_int(period) or period not in TARIFF_PERIODS:
            raise ContentError(f"{where}: 'period_days' должен быть одним из {TARIFF_PERIODS}")
        for flag in ("is_legacy", "is_active"):
            if not isinstance(item[flag], bool):
                raise ContentError(f"{where}: '{flag}' должен быть true или false")
        sort_order = item["sort_order"]
        if not _is_int(sort_order):
            raise ContentError(f"{where}: 'sort_order' должен быть целым")
        tariffs.append(
            TariffData(
                code=code,
                title=title,
                price_kopecks=price,
                period_days=period,
                is_legacy=item["is_legacy"],
                is_active=item["is_active"],
                sort_order=sort_order,
            )
        )
    return tariffs


def load_tariffs() -> list[TariffData]:
    """Загружает и кэширует tariffs.yaml; проверяет поля и значения."""
    global _TARIFFS_CACHE
    if _TARIFFS_CACHE is None:
        raw, path = _read_yaml("tariffs.yaml")
        _TARIFFS_CACHE = _validate_tariffs(raw, path)
    return _TARIFFS_CACHE


def get(key: str) -> Any:
    """Возвращает текст или словарь напоминаний по ключу."""
    data = load_texts()
    try:
        return data[key]
    except KeyError:
        raise ContentError(f"Нет текста '{key}' в {_content_path('texts.yaml')}") from None


def reload() -> None:
    """Сбрасывает кэш (для тестов и смены CONTENT_DIR)."""
    global _CACHE, _TARIFFS_CACHE
    _CACHE = None
    _TARIFFS_CACHE = None
