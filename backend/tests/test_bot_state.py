from datetime import UTC, datetime
from pathlib import Path

from app.bot.state import BotState, LiveOpenTrade, load, save
from app.schemas.backtest import PositionSide
from app.services.trading_logic import OpenTrade, PendingSetup


def test_load_returns_none_when_no_state_file(tmp_path: Path) -> None:
    assert load(str(tmp_path), "BTC-USDT") is None


def test_save_load_round_trip_with_no_setup_or_trade(tmp_path: Path) -> None:
    state = BotState(symbol="BTC-USDT")
    save(state, str(tmp_path))

    loaded = load(str(tmp_path), "BTC-USDT")
    assert loaded is not None
    assert loaded.symbol == "BTC-USDT"
    assert loaded.pending_setup is None
    assert loaded.open_trade is None


def test_save_load_round_trip_with_pending_setup(tmp_path: Path) -> None:
    setup = PendingSetup(
        side=PositionSide.LONG,
        pivot_price=100.0,
        stop_loss=99.9,
        extreme_price=110.0,
        tp2_price=130.0,
    )
    state = BotState(symbol="ETH-USDT", pending_setup=setup)
    save(state, str(tmp_path))

    loaded = load(str(tmp_path), "ETH-USDT")
    assert loaded is not None
    assert loaded.pending_setup == setup


def test_save_load_round_trip_with_open_trade(tmp_path: Path) -> None:
    trade = OpenTrade(
        sequence_no=1,
        side=PositionSide.SHORT,
        entry_price=200.0,
        entry_time=datetime(2026, 1, 1, tzinfo=UTC),
        quantity=0.5,
        is_box_trade=False,
        stop_loss=205.0,
        take_profit=190.0,
    )
    live_trade = LiveOpenTrade(
        trade=trade,
        entry_order_id="entry-1",
        sl_order_id="sl-1",
        tp_order_id="tp-1",
    )
    state = BotState(
        symbol="SOL-USDT",
        open_trade=live_trade,
        last_processed_pivot_timestamp="2026-01-01T00:00:00+00:00",
    )
    save(state, str(tmp_path))

    loaded = load(str(tmp_path), "SOL-USDT")
    assert loaded is not None
    assert loaded.open_trade is not None
    assert loaded.open_trade.entry_order_id == "entry-1"
    assert loaded.open_trade.sl_order_id == "sl-1"
    assert loaded.open_trade.tp_order_id == "tp-1"
    assert loaded.open_trade.tp1_order_id is None
    assert loaded.open_trade.trade == trade
    assert loaded.last_processed_pivot_timestamp == "2026-01-01T00:00:00+00:00"


def test_save_does_not_leave_tmp_file_behind(tmp_path: Path) -> None:
    save(BotState(symbol="BTC-USDT"), str(tmp_path))

    files = list(tmp_path.iterdir())
    assert [f.name for f in files] == ["BTC-USDT.state.json"]


def test_save_overwrites_previous_state(tmp_path: Path) -> None:
    save(BotState(symbol="BTC-USDT", last_processed_pivot_timestamp="first"), str(tmp_path))
    save(BotState(symbol="BTC-USDT", last_processed_pivot_timestamp="second"), str(tmp_path))

    loaded = load(str(tmp_path), "BTC-USDT")
    assert loaded is not None
    assert loaded.last_processed_pivot_timestamp == "second"
