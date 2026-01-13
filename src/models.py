# src/models.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import State, StatesGroup


# ----------------------------
# FSM
# ----------------------------

class UserFlow(StatesGroup):
    """
    Минимум состояний:
      - выбрана кофейня (храним в FSM data cafe_id)
      - выбор модификаторов для конкретного продукта
    """
    choosing_cafe = State()
    choosing_modifiers = State()


# ----------------------------
# CallbackData
# ----------------------------

class NavCB(CallbackData, prefix="nav"):
    page: int


class CafeCB(CallbackData, prefix="cafe"):
    cafe_id: int


class CategoryCB(CallbackData, prefix="cat"):
    cafe_id: int
    category_id: int  # 0 means "all"


class ProductCB(CallbackData, prefix="prd"):
    cafe_id: int
    product_id: int


class ModGroupCB(CallbackData, prefix="mg"):
    cafe_id: int
    product_id: int
    group_id: int


class ToggleModCB(CallbackData, prefix="mod"):
    cafe_id: int
    product_id: int
    group_id: int
    modifier_id: int


class ModsDoneCB(CallbackData, prefix="mdone"):
    cafe_id: int
    product_id: int


class AddToCartCB(CallbackData, prefix="add"):
    cafe_id: int
    product_id: int
    # modifiers_key will be stored in FSM, not in callback, to keep callback small


class CartOpenCB(CallbackData, prefix="cart"):
    cafe_id: int


class CartDelCB(CallbackData, prefix="cdel"):
    cafe_id: int
    cart_item_id: int


class CartClearCB(CallbackData, prefix="cclear"):
    cafe_id: int


class CheckoutCB(CallbackData, prefix="pay"):
    cafe_id: int


class ProfileCB(CallbackData, prefix="me"):
    pass


# ----------------------------
# Domain DTOs (used between handlers/services)
# ----------------------------

OrderStatus = Literal[
    "created",
    "pending_payment",
    "paid",
    "accepted",
    "in_progress",
    "done",
    "canceled",
]


@dataclass(slots=True)
class ModifierChoice:
    id: int
    name: str
    price_delta: str  # keep as formatted string for UI (e.g. "+40.00")


@dataclass(slots=True)
class ProductView:
    id: int
    name: str
    description: str | None
    base_price: str  # formatted
    has_modifiers: bool


@dataclass(slots=True)
class CartLineUI:
    cart_item_id: int
    title: str
    qty: int
    unit_price: str
    line_total: str
    blocked: bool
    blocked_reason: str | None


@dataclass(slots=True)
class ProfileView:
    cups_total: int
    favorite_cafe: str | None
    favorite_product: str | None


# ----------------------------
# Helpers for FSM storage
# ----------------------------

FSM_KEY_CAFE_ID = "cafe_id"

# When choosing modifiers:
FSM_KEY_PRODUCT_ID = "product_id"
FSM_KEY_SELECTED_MODS = "selected_mods"  # list[int]
FSM_KEY_GROUP_RULES = "group_rules"      # dict[group_id -> {min,max,required}]
