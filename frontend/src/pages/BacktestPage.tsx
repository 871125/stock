import { useState } from "react";
import axios from "axios";
import { CandleChart } from "../components/CandleChart";
import { runBacktest } from "../api/backtest";
import type { BacktestResult } from "../types/backtest";
import "./BacktestPage.css";

function formatNumber(value: number, digits = 2): string {
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString();
}

function isoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function defaultDateRange(): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end.getTime() - 30 * 24 * 60 * 60 * 1000);
  return { start: isoDate(start), end: isoDate(end) };
}

function extractErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => `${Array.isArray(d.loc) ? d.loc.at(-1) : "error"}: ${d.msg}`)
        .join(", ");
    }
    return err.message;
  }
  return err instanceof Error ? err.message : "Backtest failed";
}

export function BacktestPage() {
  const { start: defaultStart, end: defaultEnd } = defaultDateRange();
  const [symbol, setSymbol] = useState("BTC-USDT");
  const [startDate, setStartDate] = useState(defaultStart);
  const [endDate, setEndDate] = useState(defaultEnd);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleRun() {
    setError(null);

    if (!symbol.trim() || !startDate || !endDate) {
      setError("Please enter a symbol and both a start and end date.");
      return;
    }
    if (startDate >= endDate) {
      setError("Start date must be before end date.");
      return;
    }

    setLoading(true);
    try {
      const data = await runBacktest({
        symbol: symbol.trim(),
        start_date: startDate,
        end_date: endDate,
      });
      setResult(data);
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="backtest-page">
      <h1>Backtesting</h1>
      <div className="backtest-form">
        <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="Symbol" />
        <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
        <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
        <button onClick={handleRun} disabled={loading}>
          {loading ? "Running..." : "Run Backtest"}
        </button>
      </div>
      {error && (
        <p className="backtest-error" role="alert">
          {error}
        </p>
      )}

      {result && (
        <>
          <section className="backtest-section">
            <h2>HTF (4h) — Market Structure</h2>
            <CandleChart candles={result.htf_candles} pivots={result.htf_pivots} height={350} />
          </section>

          <section className="backtest-section">
            <h2>LTF (1h) — Entries</h2>
            <CandleChart
              candles={result.ltf_candles}
              pivots={result.ltf_pivots}
              positions={result.positions}
              height={450}
            />
          </section>

          <section className="backtest-section">
            <h2>Trades</h2>
            <div className="backtest-table-wrap">
              <table className="backtest-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Side</th>
                    <th>Entry Time</th>
                    <th>Entry</th>
                    <th>SL</th>
                    <th>TP</th>
                    <th>Size</th>
                    <th>Value</th>
                    <th>PnL</th>
                    <th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {result.positions.map((p) => (
                    <tr key={p.sequence_no}>
                      <td>{p.sequence_no}</td>
                      <td className={p.side === "long" ? "side-long" : "side-short"}>
                        {p.side.toUpperCase()}
                      </td>
                      <td>{formatTime(p.entry_time)}</td>
                      <td>{formatNumber(p.entry_price)}</td>
                      <td>{formatNumber(p.stop_loss)}</td>
                      <td>{formatNumber(p.take_profit)}</td>
                      <td>{formatNumber(p.quantity, 4)}</td>
                      <td>{formatNumber(p.position_value)}</td>
                      <td className={(p.pnl ?? 0) >= 0 ? "pnl-positive" : "pnl-negative"}>
                        {p.pnl === null ? "-" : formatNumber(p.pnl)}
                      </td>
                      <td>{p.is_win === null ? "-" : p.is_win ? "WIN" : "LOSS"}</td>
                    </tr>
                  ))}
                  {result.positions.length === 0 && (
                    <tr>
                      <td colSpan={10}>No trades in this range.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="backtest-section">
            <h2>Summary</h2>
            <div className="backtest-summary">
              <div className="backtest-summary-tile">
                <div className="label">Total Trades</div>
                <div className="value">{result.summary.total_trades}</div>
              </div>
              <div className="backtest-summary-tile">
                <div className="label">Win / Loss</div>
                <div className="value">
                  {result.summary.win_count} / {result.summary.loss_count}
                </div>
              </div>
              <div className="backtest-summary-tile">
                <div className="label">Win Rate</div>
                <div className="value">{formatNumber(result.summary.win_rate * 100, 1)}%</div>
              </div>
              <div className="backtest-summary-tile">
                <div className="label">Total PnL</div>
                <div
                  className="value"
                  style={{
                    color:
                      result.summary.total_pnl >= 0
                        ? "var(--chart-good)"
                        : "var(--chart-critical)",
                  }}
                >
                  {formatNumber(result.summary.total_pnl)}
                </div>
              </div>
              <div className="backtest-summary-tile">
                <div className="label">Max Drawdown</div>
                <div className="value">
                  {formatNumber(result.summary.max_drawdown_pct * 100, 1)}%
                </div>
              </div>
              <div className="backtest-summary-tile">
                <div className="label">Final Equity</div>
                <div className="value">{formatNumber(result.summary.final_equity)}</div>
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
