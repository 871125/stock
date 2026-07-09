"""Live bot state persistence (spec section 6.2: crash recovery via a state file).

Written after every state transition (new pending setup, order placed, TP1
partial fill, position closed) so a restarted bot picks up exactly where it
left off instead of re-deciding from scratch (which could double-enter a
position the exchange already opened). Writes are atomic (temp file + os.replace)
so a crash mid-write can never leave a corrupt/partial state file behind.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.schemas.backtest import PositionSide
from app.services.trading_logic import OpenTrade, PendingSetup

_STATE_FILE_SUFFIX = ".state.json"


@dataclass
class LiveOpenTrade:
    """An OpenTrade (shared decision fields) plus the exchange order ids the
    bot needs to manage/cancel it -- order ids have no equivalent in the
    backtester since it never places a real order."""

    trade: OpenTrade
    entry_order_id: str
    sl_order_id: str | None = None
    tp_order_id: str | None = None  # single TP (plain trend trade)
    tp1_order_id: str | None = None  # box / hybrid trend TP1 (partial)
    tp2_order_id: str | None = None  # box / hybrid trend TP2 (final)


@dataclass
class BotState:
    symbol: str
    pending_setup: PendingSetup | None = None
    open_trade: LiveOpenTrade | None = None
    # ISO timestamp of the most recently confirmed LTF pivot already acted on
    # (armed as a pending setup or superseded) -- prevents re-reacting to the
    # same pivot on the next poll after a rolling-window refetch.
    last_processed_pivot_timestamp: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def _state_path(state_dir: str, symbol: str) -> Path:
    return Path(state_dir) / f"{symbol}{_STATE_FILE_SUFFIX}"


def _open_trade_to_dict(trade: OpenTrade) -> dict:
    return {
        "sequence_no": trade.sequence_no,
        "side": trade.side.value,
        "entry_price": trade.entry_price,
        "entry_time": trade.entry_time.isoformat(),
        "quantity": trade.quantity,
        "is_box_trade": trade.is_box_trade,
        "stop_loss": trade.stop_loss,
        "take_profit": trade.take_profit,
        "take_profit_1": trade.take_profit_1,
        "take_profit_2": trade.take_profit_2,
        "tp1_hit": trade.tp1_hit,
        "remaining_fraction": trade.remaining_fraction,
        "realized_pnl": trade.realized_pnl,
    }


def _open_trade_from_dict(data: dict) -> OpenTrade:
    return OpenTrade(
        sequence_no=data["sequence_no"],
        side=PositionSide(data["side"]),
        entry_price=data["entry_price"],
        entry_time=datetime.fromisoformat(data["entry_time"]),
        quantity=data["quantity"],
        is_box_trade=data["is_box_trade"],
        stop_loss=data["stop_loss"],
        take_profit=data["take_profit"],
        take_profit_1=data["take_profit_1"],
        take_profit_2=data["take_profit_2"],
        tp1_hit=data["tp1_hit"],
        remaining_fraction=data["remaining_fraction"],
        realized_pnl=data["realized_pnl"],
    )


def _pending_setup_to_dict(setup: PendingSetup) -> dict:
    return {
        "side": setup.side.value,
        "pivot_price": setup.pivot_price,
        "stop_loss": setup.stop_loss,
        "extreme_price": setup.extreme_price,
        "tp2_price": setup.tp2_price,
    }


def _pending_setup_from_dict(data: dict) -> PendingSetup:
    return PendingSetup(
        side=PositionSide(data["side"]),
        pivot_price=data["pivot_price"],
        stop_loss=data["stop_loss"],
        extreme_price=data["extreme_price"],
        tp2_price=data["tp2_price"],
    )


def _live_open_trade_to_dict(live_trade: LiveOpenTrade) -> dict:
    return {
        "trade": _open_trade_to_dict(live_trade.trade),
        "entry_order_id": live_trade.entry_order_id,
        "sl_order_id": live_trade.sl_order_id,
        "tp_order_id": live_trade.tp_order_id,
        "tp1_order_id": live_trade.tp1_order_id,
        "tp2_order_id": live_trade.tp2_order_id,
    }


def _live_open_trade_from_dict(data: dict) -> LiveOpenTrade:
    return LiveOpenTrade(
        trade=_open_trade_from_dict(data["trade"]),
        entry_order_id=data["entry_order_id"],
        sl_order_id=data["sl_order_id"],
        tp_order_id=data["tp_order_id"],
        tp1_order_id=data["tp1_order_id"],
        tp2_order_id=data["tp2_order_id"],
    )


def save(state: BotState, state_dir: str) -> None:
    state.updated_at = datetime.now(UTC).isoformat()
    payload = {
        "symbol": state.symbol,
        "pending_setup": (
            _pending_setup_to_dict(state.pending_setup) if state.pending_setup else None
        ),
        "open_trade": (_live_open_trade_to_dict(state.open_trade) if state.open_trade else None),
        "last_processed_pivot_timestamp": state.last_processed_pivot_timestamp,
        "updated_at": state.updated_at,
    }

    path = _state_path(state_dir, state.symbol)
    os.makedirs(path.parent, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)  # atomic on both POSIX and Windows


def load(state_dir: str, symbol: str) -> BotState | None:
    path = _state_path(state_dir, symbol)
    if not path.exists():
        return None

    payload = json.loads(path.read_text(encoding="utf-8"))
    return BotState(
        symbol=payload["symbol"],
        pending_setup=(
            _pending_setup_from_dict(payload["pending_setup"]) if payload["pending_setup"] else None
        ),
        open_trade=(
            _live_open_trade_from_dict(payload["open_trade"]) if payload["open_trade"] else None
        ),
        last_processed_pivot_timestamp=payload["last_processed_pivot_timestamp"],
        updated_at=payload["updated_at"],
    )
