import { apiClient } from "./client";
import type { BacktestRequest, BacktestResult } from "../types/backtest";

export async function runBacktest(request: BacktestRequest): Promise<BacktestResult> {
  const { data } = await apiClient.post<BacktestResult>("/backtest", request);
  return data;
}
