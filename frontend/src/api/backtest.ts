import { apiClient } from "./client";
import type {
  BacktestJobCreated,
  BacktestJobStatusResponse,
  BacktestRequest,
  BacktestResult,
} from "../types/backtest";

export async function runBacktest(request: BacktestRequest): Promise<BacktestResult> {
  const { data } = await apiClient.post<BacktestResult>("/backtest", request);
  return data;
}

export async function createBacktestJob(request: BacktestRequest): Promise<string> {
  const { data } = await apiClient.post<BacktestJobCreated>("/backtest/jobs", request);
  return data.job_id;
}

export async function getBacktestJob(jobId: string): Promise<BacktestJobStatusResponse> {
  const { data } = await apiClient.get<BacktestJobStatusResponse>(`/backtest/jobs/${jobId}`);
  return data;
}
