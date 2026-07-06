export type Timeframe = "4h" | "1h" | "1m";
export type PivotType = "SH" | "SL";
export type TrendState = "uptrend" | "downtrend" | "consolidation";
export type PositionSide = "long" | "short";

export interface Candle {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface PivotPoint {
  index: number;
  timestamp: string;
  price: number;
  type: PivotType;
  sequence_no: number;
}

export interface Position {
  sequence_no: number;
  side: PositionSide;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  take_profit_1: number | null;
  quantity: number;
  position_value: number;
  pnl: number | null;
  is_win: boolean | null;
  entry_time: string;
  exit_time: string | null;
}

export interface BacktestRequest {
  symbol: string;
  start_date: string;
  end_date: string;
  initial_equity?: number;
}

export interface BacktestSummary {
  total_trades: number;
  win_count: number;
  loss_count: number;
  win_rate: number;
  total_pnl: number;
  max_drawdown_pct: number;
  final_equity: number;
}

export interface BacktestResult {
  symbol: string;
  htf_candles: Candle[];
  ltf_candles: Candle[];
  htf_pivots: PivotPoint[];
  ltf_pivots: PivotPoint[];
  positions: Position[];
  summary: BacktestSummary;
}
